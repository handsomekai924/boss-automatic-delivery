/**
 * 招呼语：全局提示词（要求 + 内置默认对照），改完保存对下次生成 / 重新生成立即生效。
 *
 * 旧页是右侧 Sheet，拆分后独立成段（与设置页各段同款）：进来才拉一次，保存后回读真实状态。
 */

import { useEffect, useState } from "react";
import { Box, Typography } from "@mui/material";

import { Button, ConfirmSheet, Surface, TextField, useToast } from "../../../components";
import { fontFamilyMono, typeScale } from "../../../theme/tokens";
import { systemApi } from "../repos";
import { errText, GREETING_MAX } from "./types";

export function GreetingSection() {
  const toast = useToast();
  const [promptCustom, setPromptCustom] = useState("");
  const [promptDefault, setPromptDefault] = useState("");
  const [promptIsDefault, setPromptIsDefault] = useState(true);
  /** 恢复默认会丢掉自定义文案——破坏性动作走二次确认（缺陷清单「破坏性操作」）。 */
  const [confirmReset, setConfirmReset] = useState(false);
  /** 网络请求在途：按钮转文案并禁用（缺陷清单「加载态」）。 */
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);

  const loadPrompt = async () => {
    try {
      // 后端回 `{requirement, custom, is_default, default}`，repos 类型写 `{prompt}`——按后端形状读
      const raw = (await systemApi.greetingPrompt()) as unknown as {
        prompt?: string;
        requirement?: string;
        custom?: string | null;
        default?: string;
        is_default?: boolean;
      };
      const custom = raw.custom ?? raw.requirement ?? raw.prompt ?? "";
      setPromptCustom(custom);
      setPromptDefault(raw.default ?? "");
      setPromptIsDefault(raw.is_default ?? !custom);
      return raw;
    } catch (e) {
      toast.toast(errText(e), "danger");
      return null;
    }
  };

  useEffect(() => {
    void loadPrompt();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const savePrompt = async () => {
    setSaving(true);
    try {
      await systemApi.saveGreetingPrompt(promptCustom.trim());
      const raw = await loadPrompt();
      // 保存后服务端仍报「内置默认」：大概率是 repos 的请求体键名没对上，别谎报成功
      if (promptCustom.trim() && raw?.is_default) {
        toast.toast("保存后仍是内置默认，没生效——看 MatchPage 文件头注释", "warn");
      } else {
        toast.toast("已保存", "ok");
      }
    } catch (e) {
      toast.toast(errText(e), "danger");
    } finally {
      setSaving(false);
    }
  };

  const resetPrompt = async () => {
    setResetting(true);
    try {
      await systemApi.saveGreetingPrompt("");
      await loadPrompt();
      toast.toast("已恢复默认", "ok");
    } catch (e) {
      toast.toast(errText(e), "danger");
    } finally {
      setResetting(false);
    }
  };

  return (
    <Box>
      {/* 表头对齐「历史分析」：左标题 + 右动作（缺陷清单「打招呼语缺左侧标题」） */}
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 1.5 }}>
        <Typography sx={{ ...typeScale.m, fontWeight: 600, flex: 1 }}>打招呼语</Typography>
        <Button size="small" variant="gray" disabled={resetting || saving} onClick={() => setConfirmReset(true)}>
          {resetting ? "恢复中…" : "恢复默认"}
        </Button>
        <Button size="small" variant="filled" disabled={saving || resetting} onClick={() => void savePrompt()}>
          {saving ? "保存中…" : "保存"}
        </Button>
      </Box>

      <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
        <TextField
          label="招呼语要求"
          multiline
          rows={4}
          fullWidth
          inputProps={{ maxLength: GREETING_MAX }}
          value={promptCustom}
          onChange={(e) => setPromptCustom(e.target.value)}
          placeholder="留空 = 用内置默认"
        />
        <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
          当前状态：{promptIsDefault ? "使用内置默认" : "使用自定义"} · 最多 {GREETING_MAX} 字
        </Typography>
        <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
          留空 = 用内置默认。保存后下次生成 / 重新生成立即生效。
        </Typography>
        <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>内置默认</Typography>
        <Surface sx={{ maxHeight: 180, overflowY: "auto" }}>
          <Typography
            component="pre"
            sx={{ ...typeScale.s, fontFamily: fontFamilyMono, whiteSpace: "pre-wrap", m: 0 }}
          >
            {promptDefault || "—"}
          </Typography>
        </Surface>
      </Box>

      <ConfirmSheet
        open={confirmReset}
        title="恢复为内置默认？"
        description="当前自定义的招呼语要求会被清掉，恢复后不可找回。"
        confirmText="恢复默认"
        danger
        onConfirm={() => {
          setConfirmReset(false);
          void resetPrompt();
        }}
        onClose={() => setConfirmReset(false)}
      />
    </Box>
  );
}
