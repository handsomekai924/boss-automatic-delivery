import type { ReactNode } from "react";
import { Box, Typography } from "@mui/material";
import { Button } from "../controls/Button";
import { Sheet } from "./Sheet";

export interface ConfirmSheetProps {
  open: boolean;
  title: ReactNode;
  /** 说明文案：讲清这个动作删什么、能不能恢复 */
  description?: ReactNode;
  confirmText?: string;
  cancelText?: string;
  /** 破坏性操作用 danger（红色主按钮） */
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

/**
 * 二次确认弹窗（删除等破坏性操作专用）：居中 Sheet + 取消/确认。
 * 替代 `window.confirm`——所有破坏性操作都走这里（缺陷清单「破坏性操作」）。
 */
export function ConfirmSheet({
  open, title, description,
  confirmText = "确认", cancelText = "取消",
  danger = false, busy = false,
  onConfirm, onClose,
}: ConfirmSheetProps) {
  return (
    <Sheet
      open={open}
      onClose={onClose}
      anchor="center"
      width={420}
      title={title}
      footer={
        <Box sx={{ display: "flex", gap: 1, justifyContent: "flex-end" }}>
          <Button variant="gray" disabled={busy} onClick={onClose}>{cancelText}</Button>
          <Button variant={danger ? "danger" : "filled"} disabled={busy} onClick={onConfirm}>
            {confirmText}
          </Button>
        </Box>
      }
    >
      {description !== undefined && (
        <Typography variant="body2" sx={{ color: "text.secondary" }}>{description}</Typography>
      )}
    </Sheet>
  );
}
