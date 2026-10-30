/**
 * 登录与会话（平台作用域 `#/:site/login`）：发码 / 扫码两个主动入口 + 三种人机喂料表单。
 *
 * 人机协作不是绕过（docs 拍板）：滑块是**你本人拖**，后端只把票据递回来；所以滑块这档
 * 新壳只给一个入口开后端帮助页（`authApi.sliderPageUrl`），不在壳里再造一套极验组件。
 *
 * 喂料三态吃 `useFeeds()`（WS `auth.need_code/need_slider/need_scan` 登记、`auth.state` 清掉），
 * 所以这页不轮询任务状态——`auth.*` 一到就换表单，`auth.state` 一到就收起。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Box, Typography } from "@mui/material";

import {
  Button, InlineAlert, PageHeader, Pill, ScrollArea, SegmentedControl, StatusDot, Surface, TextField,
  pageRootSx, useToast,
} from "../../components";
import { usePlatform } from "../../platform/PlatformProvider";
import { useFeeds, useSession, type AuthFeed } from "../../stores/sessionStore";
import { fmtClock } from "../../utils/format";
import { radius, typeScale } from "../../theme/tokens";
import { authApi, type AuthTask } from "./repos";

type Mode = "sms" | "qr";

const FEED_TITLE: Record<AuthFeed["kind"], string> = {
  code: "需要你输入短信验证码",
  slider: "需要你过一下滑块",
  scan: "需要你扫码确认",
};

const FEED_HINT: Record<AuthFeed["kind"], string> = {
  code: "验证码已经发到手机上，填进来就继续。",
  slider: "风控要求人拖一次滑块。打开下面的页面，拖完票据会自动递回来。",
  scan: "用招聘 App 扫下面的码，在手机上点确认。",
};

/** 手机号打码只做展示；提交用原值。 */
function maskPhone(phone: string): string {
  return phone.length >= 11 ? `${phone.slice(0, 3)}****${phone.slice(7)}` : phone;
}

/**
 * 登录二维码。
 *
 * 白底是扫码识别的前提（暗色主题下也得白），所以底色走 `common.white` 而不是随主题；
 * `revision` 变了才重新取图，省得每次进度推送都刷一遍。
 */
function QrImage({
  site, taskId, revision,
}: {
  site: string;
  taskId: string;
  revision: unknown;
}) {
  const v = Number(revision ?? 0) || 0;
  return (
    <Box sx={{ width: 168, height: 168, borderRadius: radius.s, overflow: "hidden", bgcolor: "common.white" }}>
      <img src={authApi.qrImageUrl(site, taskId, v)} alt="登录二维码" width={168} height={168} />
    </Box>
  );
}

/** 待喂料表单：一种 kind 一张卡，`auth.state` 到了就由 store 把它撤掉。 */
function FeedCard({
  feed, onCancel, onChanged,
}: {
  feed: AuthFeed;
  /** 取消登录是网络动作，返回 Promise 由卡片包在飞旗标（防连点）。 */
  onCancel: () => Promise<unknown>;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  /** 把后端抛的可读 message 原样弹出来，别吞。 */
  const run = useCallback(
    async (fn: () => Promise<unknown>, okMsg: string) => {
      setBusy(true);
      try {
        await fn();
        toast.toast(okMsg, "ok");
        onChanged();
      } catch (e) {
        toast.toast(e instanceof Error ? e.message : String(e), "danger");
      } finally {
        setBusy(false);
      }
    },
    [toast, onChanged],
  );

  /** 取消登录：请求在飞期间按钮转文案并禁用，报错由外层 toast。 */
  const cancel = useCallback(async () => {
    if (busy || cancelling) return;
    setCancelling(true);
    try {
      await onCancel();
    } finally {
      setCancelling(false);
    }
  }, [onCancel, busy, cancelling]);

  return (
    <Surface sx={{ mb: 1.5, border: (t) => `1px solid ${t.palette.warning.main}` }}>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, mb: 1.5 }}>
        <StatusDot tone="warn" />
        <Typography sx={{ ...typeScale.m, fontWeight: 600, flex: 1, minWidth: 0 }}>
          {FEED_TITLE[feed.kind]}
        </Typography>
        {feed.phone_masked && <Pill tone="static">{feed.phone_masked}</Pill>}
        <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>{fmtClock(feed.at)}</Typography>
      </Box>

      <Typography sx={{ ...typeScale.s, color: "text.secondary", mb: 1.5 }}>
        {FEED_HINT[feed.kind]}
      </Typography>

      {feed.kind === "code" && (
        <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center" }}>
          <TextField
            label="短信验证码"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="6 位数字"
            inputProps={{ inputMode: "numeric", maxLength: 6 }}
            sx={{ width: 180 }}
          />
          <Button
            variant="filled"
            disabled={busy || !code.trim()}
            onClick={() =>
              void run(() => authApi.submitCode(feed.site, { task_id: feed.task_id, code: code.trim() }), "已提交验证码")
            }
          >
            {busy ? "提交中…" : "提交"}
          </Button>
          <Button variant="plain" onClick={() => void cancel()} disabled={busy || cancelling}>
            {cancelling ? "取消中…" : "取消登录"}
          </Button>
        </Box>
      )}

      {feed.kind === "slider" && (
        <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center" }}>
          {/* 滑块在后端帮助页里拖（极验官方组件），这里只负责把人送过去 */}
          <Button
            variant="filled"
            disabled={busy}
            onClick={() =>
              window.open(authApi.sliderPageUrl(feed.site, feed.task_id), "_blank", "noreferrer")
            }
          >
            打开滑块页面
          </Button>
          <Button variant="plain" onClick={() => void cancel()} disabled={busy || cancelling}>
            {cancelling ? "取消中…" : "取消登录"}
          </Button>
        </Box>
      )}

      {feed.kind === "scan" && (
        <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap", alignItems: "center" }}>
          <QrImage site={feed.site} taskId={feed.task_id} revision={feed.challenge.revision} />
          <Button variant="plain" onClick={() => void cancel()} disabled={busy || cancelling}>
            {cancelling ? "取消中…" : "取消登录"}
          </Button>
        </Box>
      )}
    </Surface>
  );
}

export function LoginPage() {
  const toast = useToast();
  const { activeSiteId, activePlatform } = usePlatform();
  const session = useSession(activeSiteId);
  const feeds = useFeeds();
  const site = activeSiteId;

  const [mode, setMode] = useState<Mode>("sms");
  const [phone, setPhone] = useState("");
  const [dialCode, setDialCode] = useState("86");
  const [task, setTask] = useState<AuthTask | null>(null);
  const [busy, setBusy] = useState(false);

  /** 只看当前站点的待喂料；别的站点的料归它们自己的页面管。 */
  const myFeeds = useMemo(() => feeds.filter((f) => f.site === site), [feeds, site]);

  const caps = activePlatform?.caps.auth;
  const canSms = caps?.sms !== false;
  const canQr = caps?.qr !== false;

  const run = useCallback(
    async (fn: () => Promise<AuthTask>, okMsg: string) => {
      setBusy(true);
      try {
        const t = await fn();
        setTask(t);
        toast.toast(okMsg, "ok");
      } catch (e) {
        toast.toast(e instanceof Error ? e.message : String(e), "danger");
      } finally {
        setBusy(false);
      }
    },
    [toast],
  );

  // 站点换了就清掉上个站点留下的表单状态，免得把手机号带给别的平台
  useEffect(() => {
    setTask(null);
    setPhone("");
  }, [site]);

  return (
    <Box sx={pageRootSx}>
      <PageHeader
        title="登录与会话"
        subtitle={`${activePlatform?.display_name ?? site} · 发码登录，或用 App 扫码；风控拦住时由你本人过滑块`}
        actions={
          session ? (
            <StatusDot
              tone={session.busy ? "busy" : session.logged_in ? "ok" : "idle"}
              label={session.busy ? "登录中" : session.logged_in ? "已登录" : "未登录"}
            />
          ) : undefined
        }
      />

      {/* 内部滚动：喂料三态 / 会话状态 / 登录卡片 */}
      <ScrollArea>
        {myFeeds.length > 0 && (
          <Box sx={{ mb: 2.5 }}>
            <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1 }}>
              等你喂料
            </Typography>
            {myFeeds.map((f) => (
              <FeedCard
                key={f.task_id}
                feed={f}
                onCancel={() =>
                  authApi.cancel(site, f.task_id).catch((e: unknown) =>
                    toast.toast(e instanceof Error ? e.message : String(e), "danger"),
                  )
                }
                onChanged={() => void authApi.status(site).then((s) => setTask(s.task)).catch(() => undefined)}
              />
            ))}
          </Box>
        )}

        <Surface sx={{ mb: 2.5, maxWidth: 640 }}>
          <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1.5 }}>
            当前会话
          </Typography>
          {session?.logged_in ? (
            <Box sx={{ display: "flex", alignItems: "center", gap: 1.5 }}>
              <StatusDot tone="ok" />
              <Typography sx={{ ...typeScale.bodySm }}>
                已登录{session.phone_masked ? ` · ${session.phone_masked}` : ""}
              </Typography>
            </Box>
          ) : (
            <Box sx={{ display: "flex", alignItems: "center", gap: 1.5 }}>
              <StatusDot tone="idle" />
              <Typography sx={{ ...typeScale.bodySm, color: "text.secondary" }}>
                还没登录。下面选一种方式登录，中途要你配合的地方会自动弹出来。
              </Typography>
            </Box>
          )}
        </Surface>

        <Surface sx={{ maxWidth: 640 }}>
          <Box sx={{ mb: 2 }}>
            <SegmentedControl
              value={mode}
              onChange={(v) => setMode(v as Mode)}
              options={[
                { value: "sms", label: "手机号 + 验证码", disabled: !canSms },
                { value: "qr", label: "App 扫码", disabled: !canQr },
              ]}
            />
          </Box>

          {!canSms && !canQr && (
            <InlineAlert tone="warn">
              这个平台没声明任何登录方式（caps.auth 全为假），登录页在这里只是占位。
            </InlineAlert>
          )}

          {mode === "sms" && (
            <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "flex-end" }}>
              <TextField
                label="区号"
                value={dialCode}
                onChange={(e) => setDialCode(e.target.value)}
                sx={{ width: 96 }}
                disabled={busy}
              />
              <TextField
                label="手机号"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                inputProps={{ inputMode: "tel", maxLength: 11 }}
                sx={{ width: 200 }}
                disabled={busy}
              />
              <Button
                variant="filled"
                disabled={busy || phone.trim().length < 11}
                onClick={() =>
                  void run(
                    () => authApi.sendSms(site, { phone: phone.trim(), dial_code: dialCode.trim() || "86" }),
                    `验证码已发到 ${maskPhone(phone.trim())}`,
                  )
                }
              >
                {busy ? "发送中…" : "发送验证码"}
              </Button>
            </Box>
          )}

          {mode === "qr" && (
            <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center" }}>
              <Button
                variant="filled"
                disabled={busy}
                onClick={() => void run(() => authApi.startQr(site), "二维码已生成")}
              >
                {busy ? "生成中…" : "生成二维码"}
              </Button>
              {task?.qr?.active && (
                <QrImage site={site} taskId={task.task_id} revision={task.qr?.revision} />
              )}
            </Box>
          )}

          {task && !task.qr?.active && (
            <Box sx={{ mt: 2, display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center" }}>
              <Pill tone="static">任务 {task.task_id}</Pill>
              <Pill tone={task.status === "error" ? "danger" : "accent"}>{task.status}</Pill>
              {task.error && (
                <Typography sx={{ ...typeScale.s, color: "error.main" }}>{task.error}</Typography>
              )}
              <Button
                variant="plain"
                disabled={busy}
                onClick={() => void run(() => authApi.cancel(site, task.task_id).then(() => task), "已取消")}
              >
                {busy ? "取消中…" : "取消登录"}
              </Button>
            </Box>
          )}
        </Surface>

        {caps?.slider && (
          <Typography sx={{ ...typeScale.s, color: "text.secondary", mt: 2 }}>
            这个平台可能触发风控滑块——出现时上面会自动出一张「需要你过一下滑块」的卡，
            拖完票据由后端递回，不需要你做别的。
          </Typography>
        )}
      </ScrollArea>
    </Box>
  );
}
