"""命令行层测试：每个子命令都真跑一遍并核对退出码。"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

import boss_login.cli as cli
from boss_login.client import ZhipinLoginClient
from tools.mock_server import STORE

PHONE = "13800138000"


def run(argv: list[str]) -> int:
    """跑一次 CLI，返回退出码。"""
    return cli.main(argv)


def base(server: str, session: Path, *rest: str) -> list[str]:
    """拼一套 CLI 公共参数。"""
    return ["--base-url", server, "--db", str(session), *rest]


def seed_code(server: str, phone: str, **kwargs) -> str:
    """用独立客户端发一次码，取回假服务端生成的验证码。

    Args:
        server: 假服务端地址。
        phone: 手机号。
        **kwargs: 透传给发码接口的附加参数。

    Returns:
        假服务端记录的验证码。
    """
    ZhipinLoginClient(base_url=server, retries=0, timeout=5.0, sleeper=lambda _s: None).send_sms_code(
        phone, force=True, **kwargs
    )
    return STORE[phone]["code"]


def code_from_store(phone: str):
    """造一个 `input()` 替身，提示输码时从 STORE 现取验证码。"""

    def _input(_prompt: str = "") -> str:
        """返回 STORE 里记下的验证码。"""
        return STORE[phone]["code"]

    return _input


@pytest.fixture
def session(tmp_path: Path) -> Path:
    """指到临时状态库。"""
    return tmp_path / "boss.db"


class TestEntrypoint:
    """CLI 入口与全局参数解析。"""

    def test_all_subcommands_are_registered(self):
        """子命令都已注册。"""
        parser = cli.build_parser()
        for name in ("login", "whoami", "logout", "probe"):
            assert parser.parse_args([name, *(["--phone", PHONE] if name == "login" else [])])

    @pytest.mark.parametrize(
        "argv",
        [
            ["-v", "login", "--phone", PHONE],
            ["login", "--phone", PHONE, "-v"],
            ["login", "-v", "--phone", PHONE],
            ["whoami", "-v"],
        ],
    )
    def test_verbose_flag_works_before_or_after_subcommand(self, argv):
        """`-v` 在子命令前后都能解析。"""
        args = cli.build_parser().parse_args(argv)
        assert args.verbose is True

    def test_global_options_given_late_win_over_defaults(self):
        """后给的全局选项覆盖默认值，未给的保持 `None`。"""
        args = cli.build_parser().parse_args(
            ["login", "--phone", PHONE, "--timeout", "5", "--base-url", "http://127.0.0.1:9"]
        )
        assert args.timeout == 5.0
        assert args.base_url == "http://127.0.0.1:9"
        assert args.db is None

    def test_default_db_path_is_importable(self):
        """`DEFAULT_DB_PATH` 可从 CLI 模块导入且与 session 一致。"""
        from boss_login.cli import DEFAULT_DB_PATH
        from boss_login.session import DEFAULT_DB_PATH as real

        assert DEFAULT_DB_PATH == real

    def test_default_db_lives_in_project_data_dir(self):
        """状态库落在项目根 `data/` 下，按包位置定位，不看 cwd。"""
        import boss_login
        import boss_db
        from boss_login.session import DEFAULT_DB_PATH, PROJECT_ROOT

        package_dir = Path(boss_login.__file__).resolve().parent

        assert PROJECT_ROOT == package_dir.parent, "PROJECT_ROOT 应该是包目录的上一级"
        assert DEFAULT_DB_PATH == boss_db.DEFAULT_DB_PATH
        assert DEFAULT_DB_PATH == PROJECT_ROOT / "data" / "boss.db"
        assert DEFAULT_DB_PATH.is_absolute()
        assert not str(DEFAULT_DB_PATH).startswith(str(Path.home()))
        assert str(DEFAULT_DB_PATH).startswith(str(package_dir.parent))


class TestLoginCommand:
    """login 子命令的登录与落盘。"""

    def test_full_login_writes_session(self, server, session, capsys):
        """完整登录成功并写出可读回的会话。"""
        code = seed_code(server, PHONE)

        assert run(base(server, session, "login", "--phone", PHONE, "--code", code)) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "登录成功" in out
        assert "求职者8000" in out
        assert "首次验证" in out

        from boss_login.session import load_session

        stored = load_session(session)
        assert stored.phone_masked == "138****8000"
        assert stored.token == ""
        assert stored.cookies["zp_at"].startswith("mock-token-")

    def test_no_save_skips_writing_session(self, server, session):
        """`--no-save` 不写会话。"""
        code = seed_code(server, PHONE)
        argv = base(server, session, "login", "--phone", PHONE, "--code", code, "--no-save")
        assert run(argv) == cli.EXIT_OK
        from boss_login.session import has_session_row

        assert has_session_row(session) is False

    def test_eof_at_prompt_cancels_cleanly(self, server, session, monkeypatch, capsys):
        """交互式输入被中断时应干净退出，不是抛栈。"""

        def raise_eof(_prompt=""):
            """模拟输入被中断。"""
            raise EOFError

        monkeypatch.setattr(builtins, "input", raise_eof)
        assert run(base(server, session, "login", "--phone", PHONE)) == cli.EXIT_ERROR
        assert "已取消登录" in capsys.readouterr().err

    def test_wrong_then_right_code_via_prompt(self, server, session, monkeypatch):
        """先错码再对码仍可登录成功。"""
        correct = seed_code(server, PHONE)
        answers = iter(["000000", correct])
        monkeypatch.setattr(builtins, "input", lambda _p="": next(answers))

        assert run(base(server, session, "login", "--phone", PHONE)) == cli.EXIT_OK

    def test_prompts_after_sending_code(self, server, session, monkeypatch, capsys):
        """一次 login 就是「发码 → 输码」，发码那步的反馈不能省。"""
        monkeypatch.setattr(builtins, "input", code_from_store(PHONE))

        assert run(base(server, session, "login", "--phone", PHONE)) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "验证码已发送至" in out
        assert "138****8000" in out


class TestExitCodes:
    """参数 / 风控 / 封禁场景的退出码。"""

    def test_invalid_phone_exits_with_validation_code(self, server, session, capsys):
        """非法手机号走校验退出码。"""
        assert run(base(server, session, "login", "--phone", "12345")) == cli.EXIT_VALIDATION
        assert "手机号格式不正确" in capsys.readouterr().err

    def test_risk_control_exits_with_risk_code(self, server, session, capsys):
        """风控场景走风控退出码并给出验证入口。"""
        code = run(base(server, session, "login", "--phone", "13800138010"))
        assert code == cli.EXIT_RISK_CONTROL
        err = capsys.readouterr().err
        assert "风控" in err
        assert "seed=mock-seed" in err
        assert "verify.html" in err
        assert "--verify-token" in err

    def test_verify_token_unblocks_risk_control(self, server, session, monkeypatch):
        """`--verify-token` 可解开 SECURITY_CHECK 类风控。"""
        monkeypatch.setattr(builtins, "input", code_from_store("13800138010"))
        argv = base(server, session, "login", "--phone", "13800138010", "--verify-token", "human")
        assert run(argv) == cli.EXIT_OK

    def test_slider_challenge_reports_risk_not_generic_error(self, server, session, capsys):
        """滑块挑战报风控话术，且不再误导走 `--verify-token`。"""
        argv = base(server, session, "login", "--phone", "13800138020", "--no-helper")
        assert run(argv) == cli.EXIT_RISK_CONTROL
        err = capsys.readouterr().err
        assert "风控" in err
        assert "400061" in err
        assert "滑块验证" in err
        assert "重跑一次" in err
        assert "--verify-token" not in err

    def test_unknown_challenge_payload_is_echoed_back(self, server, session, capsys):
        """认不出的挑战参数原样回显，便于适配。"""
        run(base(server, session, "login", "--phone", "13800138020", "--no-helper"))
        err = capsys.readouterr().err
        assert "原始返回" in err
        assert "mock-challenge-id" in err
        assert "mock-gt" in err

    def test_plain_failure_message_stays_a_generic_error(self, server, session, capsys):
        """对照组：普通验证码失败不报成风控。"""
        assert run(base(server, session, "login", "--phone", "13800139010")) == cli.EXIT_ERROR
        err = capsys.readouterr().err
        assert "风控" not in err
        assert "code=1004" in err

    def test_verify_token_does_not_unblock_the_slider(self, server, session):
        """`--verify-token` 解不开滑块挑战。"""
        argv = base(
            server, session, "login", "--phone", "13800138020",
            "--verify-token", "solved", "--no-helper",
        )
        assert run(argv) == cli.EXIT_RISK_CONTROL

    def test_blocked_phone_exits_with_blocked_code(self, server, session, capsys):
        """被封手机号走封禁退出码。"""
        assert run(base(server, session, "login", "--phone", "13800000000")) == cli.EXIT_BLOCKED
        assert "已被限制" in capsys.readouterr().err


SLIDER_PHONE = "13800138020"


class TestSliderCommand:
    """login 命中滑块时的自动衔接。"""

    def test_login_auto_solves_slider_on_the_send_step(self, server, session, monkeypatch):
        """默认人机协作过掉滑块并自动重试发码。"""
        from boss_login.verify import SliderSolution
        from tools.mock_server import LAST_SMS_REQUEST, STORE

        def fake_solve(challenge, **_kwargs):
            """返回固定滑块票据。"""
            return SliderSolution(
                challenge=challenge.challenge, validate="v", seccode="s"
            )

        monkeypatch.setattr(cli, "solve_via_helper", fake_solve)
        seen_codes: list[str] = []

        def fake_input(_prompt: str = "") -> str:
            """返回 STORE 里的验证码并记下调用。"""
            code = STORE[SLIDER_PHONE]["code"]
            seen_codes.append(code)
            return code

        monkeypatch.setattr(builtins, "input", fake_input)
        assert run(base(server, session, "login", "--phone", SLIDER_PHONE)) == cli.EXIT_OK
        assert seen_codes, "发码之后才轮到输码；STORE 里应当已有验证码"
        assert LAST_SMS_REQUEST["path"] == "smsCodeV2"
        assert LAST_SMS_REQUEST["form"].get("validate")
        assert LAST_SMS_REQUEST["form"].get("challenge")
        assert LAST_SMS_REQUEST["form"].get("seccode")

    def test_login_auto_solves_slider_and_retries_phone_v2(self, server, session, monkeypatch):
        """登录步同样过滑块，重试打 phoneV2 并带票据。"""
        from boss_login.verify import SliderSolution
        from tools.mock_server import LAST_LOGIN_REQUEST

        def fake_solve(challenge, **_kwargs):
            """返回固定滑块票据。"""
            return SliderSolution(
                challenge=challenge.challenge, validate="v", seccode="s"
            )

        code = seed_code(server, SLIDER_PHONE, slider_solver=fake_solve)

        monkeypatch.setattr(cli, "solve_via_helper", fake_solve)
        assert (
            run(base(server, session, "login", "--phone", SLIDER_PHONE, "--code", code))
            == cli.EXIT_OK
        )
        assert LAST_LOGIN_REQUEST["path"] == "phoneV2"
        form = LAST_LOGIN_REQUEST["form"]
        assert form.get("validate") and form.get("challenge") and form.get("seccode")
        assert form.get("phoneCode") == code
        assert "phone" not in form
        assert "version" not in form

    def test_no_helper_keeps_the_old_hand_off(self, server, session, capsys):
        """`--no-helper` 时把决定权交回人：报风控，不自作主张解题。"""
        argv = base(server, session, "login", "--phone", SLIDER_PHONE, "--no-helper")
        assert run(argv) == cli.EXIT_RISK_CONTROL

    def test_helper_port_zero_means_system_assigned(self):
        """`--helper-port 0` 是「交给系统分配」，不能被默认值吃掉。"""
        from boss_login.config import SLIDER_HELPER_PORT

        args = cli.build_parser().parse_args(["login", "--phone", PHONE, "--helper-port", "0"])
        solver = cli._make_solver(args)
        assert solver is not None
        closed = [cell.cell_contents for cell in (solver.__closure__ or ())]
        assert 0 in closed
        assert SLIDER_HELPER_PORT not in closed

    def test_helper_timeout_takes_the_flag(self):
        """`--helper-timeout` 覆盖默认超时。"""
        args = cli.build_parser().parse_args(["login", "--phone", PHONE, "--helper-timeout", "12"])
        solver = cli._make_solver(args)
        closed = [cell.cell_contents for cell in (solver.__closure__ or ())]
        assert 12 in closed


class TestSessionCommands:
    """whoami / logout / probe 子命令。"""

    def test_whoami_without_session_fails(self, server, session, capsys):
        """无本地会话时 whoami 报「没有登录态」并说明查了哪儿。"""
        assert run(base(server, session, "whoami")) == cli.EXIT_ERROR
        out = capsys.readouterr().out
        assert "本地没有登录态" in out
        assert str(session) in out
        assert "库里没有登录态" in out

    def test_whoami_on_empty_session_row(self, server, session, capsys):
        """库有空会话行时 whoami 说清「有登录态」但不可用。"""
        from boss_login.session import StoredSession, save_session

        save_session(StoredSession(), session)
        assert run(base(server, session, "whoami")) == cli.EXIT_ERROR
        out = capsys.readouterr().out
        assert "库里有登录态" in out

    def test_whoami_when_server_rejects_stale_session(self, server, session, capsys):
        """本地认、服务端不认——说清是「需要重新登录」。"""
        from boss_login.session import save_session, StoredSession

        save_session(StoredSession(cookies={"__zp_stoken__": "stale"}), session)
        assert run(base(server, session, "whoami")) == cli.EXIT_ERROR
        out = capsys.readouterr().out
        assert "服务端已不认" in out

    def test_whoami_after_login(self, server, session, capsys):
        """登录后 whoami 显示昵称与掩码手机号。"""
        code = seed_code(server, PHONE)
        run(base(server, session, "login", "--phone", PHONE, "--code", code))
        capsys.readouterr()

        assert run(base(server, session, "whoami")) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "求职者8000" in out
        assert "138****8000" in out

    def test_logout_clears_session_and_server_state(self, server, session):
        """logout 清掉本地会话且 whoami 随之失败。"""
        from boss_login.session import has_session_row, load_session

        code = seed_code(server, PHONE)
        run(base(server, session, "login", "--phone", PHONE, "--code", code))
        assert has_session_row(session)

        assert run(base(server, session, "logout")) == cli.EXIT_OK
        assert has_session_row(session) is False
        assert load_session(session).is_empty
        assert run(base(server, session, "whoami")) == cli.EXIT_ERROR

    def test_logout_when_nothing_saved(self, server, session, capsys):
        """无会话时 logout 也成功并提示。"""
        assert run(base(server, session, "logout")) == cli.EXIT_OK
        assert "本就没有登录态" in capsys.readouterr().out

    def test_probe_reports_live_and_dead_routes(self, server, session, capsys):
        """probe 汇报通断路由。"""
        assert run(base(server, session, "probe")) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "/wapi/zppassport/send/smsCode" in out
        assert "✓" in out and "✗" in out

    def test_probe_json_is_parseable(self, server, session, capsys):
        """`--json` 的 stdout 能从首个 `[` 起解码出报告。"""
        assert run(base(server, session, "probe", "--json")) == cli.EXIT_OK
        captured = capsys.readouterr()

        assert "正在探测" not in captured.out, "进度提示不该出现在 --json 的 stdout 里"
        assert "正在探测" in captured.err

        start = captured.out.index("[\n")
        report, _end = json.JSONDecoder().raw_decode(captured.out[start:])
        assert any(item["path"].endswith("send/smsCode") and item["alive"] for item in report)


class TestConsoleEncoding:
    """输出在非 UTF-8 控制台下的健壮性。"""

    def test_announce_survives_gbk_console(self, monkeypatch):
        """GBK 控制台编码失败时 `announce` 降级打印，不抛栈。"""
        from tools.mock_server import _announce

        printed: list[str] = []

        def fake_print(message, **_kwargs):
            """GBK 控制台下的 `print` 替身。"""
            if any(ord(ch) > 127 for ch in message):
                raise UnicodeEncodeError("gbk", message, 0, 1, "illegal multibyte sequence")
            printed.append(message)

        monkeypatch.setattr(builtins, "print", fake_print)
        _announce("[mock] 📱 发给 13800138000 的验证码是 123456")

        assert printed and "123456" in printed[0]
