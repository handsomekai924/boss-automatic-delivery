import type { StatusTone } from "../components/data/StatusDot";

/** 长任务状态机视图（5.3 TaskPanel 行：状态 → 外观映射表）。 */
export type TaskState = "idle" | "running" | "done" | "error" | "cancelled";

export interface TaskStateMeta {
  label: string;
  tone: StatusTone;
  /** 进度条是否活动（busy 脉冲等） */
  active: boolean;
}

export const TASK_STATE_META: Record<TaskState, TaskStateMeta> = {
  idle: { label: "待命", tone: "idle", active: false },
  running: { label: "进行中", tone: "busy", active: true },
  done: { label: "已完成", tone: "ok", active: false },
  error: { label: "失败", tone: "danger", active: false },
  cancelled: { label: "已停止", tone: "warn", active: false },
};

/** 后端 status 字符串 → TaskState（沿用 legacy taskStateOf 分类）。 */
export function taskStateOf(status: string | null | undefined): TaskState {
  const s = String(status ?? "").toLowerCase();
  if (["running", "pending", "sending_sms", "sending", "need_code", "need_slider", "logging_in", "working", "process"].includes(s)) return "running";
  if (["done", "ok", "success", "finished", "draft"].includes(s)) return "done";
  if (["error", "failed", "fail"].includes(s)) return "error";
  if (["cancelled", "canceled", "stopped", "cancel"].includes(s)) return "cancelled";
  if (["idle", "none", ""].includes(s)) return "idle";
  return "running";
}
