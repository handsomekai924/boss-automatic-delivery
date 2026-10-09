"""命令行层测试。

单独成一个模块是有原因的：cli.py 曾经把一个不存在于 config 的名字导入进来
（``from .config import DEFAULT_SESSION_PATH``，当时还没有这个名字），整套测试却
全绿——因为没有任何一个测试 import 过 cli。库是对的，入口是坏的，而入口没人跑。
这里把每个子命令都真的执行一遍，并核对退出码。
"""

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
    return ["--base-url", server, "--db", str(session), *rest]


def seed_code(server: str, phone: str, **kwargs) -> str:
    """用库接口发一次码，取回假服务端生成的验证码。

    CLI 只保留 login/whoami/logout/probe，没有独立的发码命令了；而 login 的
    ``--code`` 非交互路径要求事先就知道验证码，所以这一步从库里发。
    换成单独的客户端实例是有意的：不会把票据/冷却状态带进随后的 CLI 登录。
    """
    ZhipinLoginClient(base_url=server, retries=0, timeout=5.0, sleeper=lambda _s: None).send_sms_code(
        phone, force=True, **kwargs
    )
    return STORE[phone]["code"]


def code_from_store(phone: str):
    """``input()`` 替身：发码已完成，提示输码时从 STORE 现取验证码。"""

    def _input(_prompt: str = "") -> str:
        return STORE[phone]["code"]

    return _input


@pytest.fixture
def session(tmp_path: Path) -> Path:
    """临时状态库，避免碰到真实的 ``data/boss.db``。"""
    return tmp_path / "boss.db"




class TestEntrypoint:
    def test_all_subcommands_are_registered(self):
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
        """`login --phone X -v` 是人的自然写法，不能因为 argparse 的位置规则报错。

        子解析器默认会用自己那份默认值盖掉顶层同名选项，所以子命令侧那份
        必须用 SUPPRESS 当默认值。
        """
        args = cli.build_parser().parse_args(argv)
        assert args.verbose is True

    def test_global_options_given_late_win_over_defaults(self):
        """后写的全局选项赢过默认值；没给的仍是「不覆盖」，让 ``BOSS_DB`` / 默认库说了算。"""
        args = cli.build_parser().parse_args(
            ["login", "--phone", PHONE, "--timeout", "5", "--base-url", "http://127.0.0.1:9"]
        )
        assert args.timeout == 5.0
        assert args.base_url == "http://127.0.0.1:9"
        assert args.db is None

    def test_default_db_path_is_importable(self):
        """这个常量曾在家门口导错模块，直接钉住它。不再藏在 home 下；跟 cwd 无关——由 ``__file__`` 推出。"""
        from boss_login.cli import DEFAULT_DB_PATH
        from boss_login.session import DEFAULT_DB_PATH as real

        assert DEFAULT_DB_PATH == real

    def test_default_db_lives_in_project_data_dir(self):
        """状态库落在项目根的 data/ 下，按包位置定位，不看 cwd。

        曾经在 ``~/.boss_login/`` 下，换个目录跑 ``python -m boss_login`` 就会
        读写错文件——whoami 报「本地没有登录态」，而登录明明刚成功。
        """
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
    def test_full_login_writes_session(self, server, session, capsys):
        """登录成功后落盘可读回。真实站点鉴权靠 Cookie（响应体无 token），假服务端照办。"""
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
        code = seed_code(server, PHONE)
        argv = base(server, session, "login", "--phone", PHONE, "--code", code, "--no-save")
        assert run(argv) == cli.EXIT_OK
        from boss_login.session import has_session_row

        assert has_session_row(session) is False

    def test_eof_at_prompt_cancels_cleanly(self, server, session, monkeypatch, capsys):
        """交互式输入被中断时应干净退出，不是抛栈。不给 ``--code`` 走交互路径。"""

        def raise_eof(_prompt=""):
            raise EOFError

        monkeypatch.setattr(builtins, "input", raise_eof)
        assert run(base(server, session, "login", "--phone", PHONE)) == cli.EXIT_ERROR
        assert "已取消登录" in capsys.readouterr().err

    def test_wrong_then_right_code_via_prompt(self, server, session, monkeypatch):
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
    def test_invalid_phone_exits_with_validation_code(self, server, session, capsys):
        assert run(base(server, session, "login", "--phone", "12345")) == cli.EXIT_VALIDATION
        assert "手机号格式不正确" in capsys.readouterr().err

    def test_risk_control_exits_with_risk_code(self, server, session, capsys):
        """SECURITY_CHECK(37) 走另一套协议，票据才是 ``--verify-token`` 传回来的。"""
        code = run(base(server, session, "login", "--phone", "13800138010"))
        assert code == cli.EXIT_RISK_CONTROL
        err = capsys.readouterr().err
        assert "风控" in err
        assert "seed=mock-seed" in err
        assert "verify.html" in err
        assert "--verify-token" in err

    def test_verify_token_unblocks_risk_control(self, server, session, monkeypatch):
        monkeypatch.setattr(builtins, "input", code_from_store("13800138010"))
        argv = base(server, session, "login", "--phone", "13800138010", "--verify-token", "human")
        assert run(argv) == cli.EXIT_OK

    def test_slider_challenge_reports_risk_not_generic_error(self, server, session, capsys):
        """真实站点实测形状：code=400061 / "请完成滑块验证"。

        修之前这里会退化成「接口返回失败（code=400061）」，用户完全不知道
        下一步该干什么。

        处理方式那段曾经错写成「用 --verify-token 传回来」——那是
        SECURITY_CHECK(37) 的路，滑块的票据是 challenge/validate/seccode
        三件套、由帮助页自动回传，压根不走 --verify-token。照着那句去做
        只会白跑一趟，所以这里钉住它**不**再出现。

        ``--no-helper`` 是关键：不给的话 CLI 会拉起本地帮助页等人拖滑块，
        测试就挂在那里等 300 秒超时了。
        """
        argv = base(server, session, "login", "--phone", "13800138020", "--no-helper")
        assert run(argv) == cli.EXIT_RISK_CONTROL
        err = capsys.readouterr().err
        assert "风控" in err
        assert "400061" in err
        assert "滑块验证" in err
        assert "重跑一次" in err
        assert "--verify-token" not in err

    def test_unknown_challenge_payload_is_echoed_back(self, server, session, capsys):
        """认不出滑块参数时，必须把原始 zpData 原样打出来。

        真实响应里那个字段叫什么名字离线没核实过；不打出来就永远没法适配。
        """
        run(base(server, session, "login", "--phone", "13800138020", "--no-helper"))
        err = capsys.readouterr().err
        assert "原始返回" in err
        assert "mock-challenge-id" in err
        assert "mock-gt" in err

    def test_plain_failure_message_stays_a_generic_error(self, server, session, capsys):
        """对照组：话术带「验证码」的普通失败不能报成风控。"""
        assert run(base(server, session, "login", "--phone", "13800139010")) == cli.EXIT_ERROR
        err = capsys.readouterr().err
        assert "风控" not in err
        assert "code=1004" in err

    def test_verify_token_does_not_unblock_the_slider(self, server, session):
        """``--verify-token`` 是 SECURITY_CHECK(37) 那条路的，不是滑块的。

        滑块票据只有表单三键 ``challenge`` / ``validate`` / ``seccode``，而且必须
        挂在 ``smsCodeV2`` 上。拿 ``verifyToken`` 去，真机照样 400061。

        早期假服务端把 ``verifyToken`` 当滑块票据，这条死路就被测成了绿的；
        契约以登录页 chunk 为准。``--no-helper`` 顺带保证不会挂 300 秒等人拖滑块。
        """
        argv = base(
            server, session, "login", "--phone", "13800138020",
            "--verify-token", "solved", "--no-helper",
        )
        assert run(argv) == cli.EXIT_RISK_CONTROL

    def test_blocked_phone_exits_with_blocked_code(self, server, session, capsys):
        assert run(base(server, session, "login", "--phone", "13800000000")) == cli.EXIT_BLOCKED
        assert "已被限制" in capsys.readouterr().err




SLIDER_PHONE = "13800138020"


class TestSliderCommand:
    def test_login_auto_solves_slider_on_the_send_step(self, server, session, monkeypatch):
        """默认就走人机协作：发现滑块并自动解题，不要求用户先填 --verify-token。

        登录成功后验证码一次性失效，STORE 会清掉那条——所以看发码请求。
        重试那次必须打 ``smsCodeV2`` 并带极验三件套，只调 validate 过不去。
        """
        from boss_login.verify import SliderSolution
        from tools.mock_server import LAST_SMS_REQUEST, STORE

        def fake_solve(challenge, **_kwargs):
            return SliderSolution(
                challenge=challenge.challenge, validate="v", seccode="s"
            )

        monkeypatch.setattr(cli, "solve_via_helper", fake_solve)
        seen_codes: list[str] = []

        def fake_input(_prompt: str = "") -> str:
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
        """登录那步跟发码一样要过滑块：重试打 phoneV2，票据在表单里。

        真机跑出来的形状是「发码过了、登录仍 400061」——登录页 Ce() 把
        verifyInfo 原样挂到 login 上，所以那一步也必须自带票据。

        先用库发码（另开客户端，不共享票据状态），CLI 进来时服务端已在冷却：
        发码再撞一次滑块并解题，靠 1001 复用票据进输码。``je()`` 换掉 phone、
        ``Ce()`` 不传 ``t``（故无 version:1）。
        """
        from boss_login.verify import SliderSolution
        from tools.mock_server import LAST_LOGIN_REQUEST

        def fake_solve(challenge, **_kwargs):
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
        """--no-helper 时把决定权交回人：报风控，不自作主张解题。"""
        argv = base(server, session, "login", "--phone", SLIDER_PHONE, "--no-helper")
        assert run(argv) == cli.EXIT_RISK_CONTROL

    def test_helper_port_zero_means_system_assigned(self):
        """--helper-port 0 是「交给系统分配」，不能被 `or 默认值` 吃掉。"""
        from boss_login.config import SLIDER_HELPER_PORT

        args = cli.build_parser().parse_args(["login", "--phone", PHONE, "--helper-port", "0"])
        solver = cli._make_solver(args)
        assert solver is not None
        closed = [cell.cell_contents for cell in (solver.__closure__ or ())]
        assert 0 in closed
        assert SLIDER_HELPER_PORT not in closed

    def test_helper_timeout_takes_the_flag(self):
        args = cli.build_parser().parse_args(["login", "--phone", PHONE, "--helper-timeout", "12"])
        solver = cli._make_solver(args)
        closed = [cell.cell_contents for cell in (solver.__closure__ or ())]
        assert 12 in closed




class TestSessionCommands:
    def test_whoami_without_session_fails(self, server, session, capsys):
        """一句话「没有登录态」没法排查：得说清查了哪儿、为什么不算登录。"""
        assert run(base(server, session, "whoami")) == cli.EXIT_ERROR
        out = capsys.readouterr().out
        assert "本地没有登录态" in out
        assert str(session) in out
        assert "库里没有登录态" in out

    def test_whoami_on_empty_session_row(self, server, session, capsys):
        from boss_login.session import StoredSession, save_session

        save_session(StoredSession(), session)
        assert run(base(server, session, "whoami")) == cli.EXIT_ERROR
        out = capsys.readouterr().out
        assert "库里有登录态" in out

    def test_whoami_when_server_rejects_stale_session(self, server, session, capsys):
        """本地认、服务端不认——得说清是「需要重新登录」，不是「没有登录态」。"""
        from boss_login.session import save_session, StoredSession

        save_session(StoredSession(cookies={"__zp_stoken__": "stale"}), session)
        assert run(base(server, session, "whoami")) == cli.EXIT_ERROR
        out = capsys.readouterr().out
        assert "服务端已不认" in out

    def test_whoami_after_login(self, server, session, capsys):
        code = seed_code(server, PHONE)
        run(base(server, session, "login", "--phone", PHONE, "--code", code))
        capsys.readouterr()

        assert run(base(server, session, "whoami")) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "求职者8000" in out
        assert "138****8000" in out

    def test_logout_clears_session_and_server_state(self, server, session):
        from boss_login.session import has_session_row, load_session

        code = seed_code(server, PHONE)
        run(base(server, session, "login", "--phone", PHONE, "--code", code))
        assert has_session_row(session)

        assert run(base(server, session, "logout")) == cli.EXIT_OK
        assert has_session_row(session) is False
        assert load_session(session).is_empty
        assert run(base(server, session, "whoami")) == cli.EXIT_ERROR

    def test_logout_when_nothing_saved(self, server, session, capsys):
        assert run(base(server, session, "logout")) == cli.EXIT_OK
        assert "本就没有登录态" in capsys.readouterr().out

    def test_probe_reports_live_and_dead_routes(self, server, session, capsys):
        assert run(base(server, session, "probe")) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "/wapi/zppassport/send/smsCode" in out
        assert "✓" in out and "✗" in out

    def test_probe_json_is_parseable(self, server, session, capsys):
        """--json 的 stdout 必须是干净 JSON，可以直接管道给 jq。

        假服务端跑在同一进程里，它的访问日志会混进 stdout，所以这里从第一个
        ``[`` 开始解码，而不是要求整段输出都是 JSON；进度提示则必须走去 stderr。
        """
        assert run(base(server, session, "probe", "--json")) == cli.EXIT_OK
        captured = capsys.readouterr()

        assert "正在探测" not in captured.out, "进度提示不该出现在 --json 的 stdout 里"
        assert "正在探测" in captured.err

        start = captured.out.index("[\n")
        report, _end = json.JSONDecoder().raw_decode(captured.out[start:])
        assert any(item["path"].endswith("send/smsCode") and item["alive"] for item in report)




class TestConsoleEncoding:
    def test_announce_survives_gbk_console(self, monkeypatch):
        """Windows 控制台是 GBK，emoji 会抛 UnicodeEncodeError。

        假服务端曾在写完冷却记录之后打印验证码，编码失败直接把请求打成 500，
        客户端一重试就撞上刚写进去的限流——一个纯日志问题伪装成业务故障。
        """
        from tools.mock_server import _announce

        printed: list[str] = []

        def fake_print(message, **_kwargs):
            if any(ord(ch) > 127 for ch in message):
                raise UnicodeEncodeError("gbk", message, 0, 1, "illegal multibyte sequence")
            printed.append(message)

        monkeypatch.setattr(builtins, "print", fake_print)
        _announce("[mock] 📱 发给 13800138000 的验证码是 123456")

        assert printed and "123456" in printed[0]
