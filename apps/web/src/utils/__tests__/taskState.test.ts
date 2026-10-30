import { describe, expect, it } from "vitest";
import { TASK_STATE_META, taskStateOf } from "../taskState";

describe("taskStateOf", () => {
  it("归一后端 status 字符串", () => {
    expect(taskStateOf("running")).toBe("running");
    expect(taskStateOf("sending_sms")).toBe("running");
    expect(taskStateOf("need_code")).toBe("running");
    expect(taskStateOf("done")).toBe("done");
    expect(taskStateOf("success")).toBe("done");
    expect(taskStateOf("failed")).toBe("error");
    expect(taskStateOf("canceled")).toBe("cancelled");
    expect(taskStateOf("")).toBe("idle");
    expect(taskStateOf(null)).toBe("idle");
    expect(taskStateOf("weird")).toBe("running"); // 未知默认按进行中
  });

  it("状态映射表覆盖全状态", () => {
    for (const s of ["idle", "running", "done", "error", "cancelled"] as const) {
      expect(TASK_STATE_META[s].label).toBeTruthy();
      expect(TASK_STATE_META[s].tone).toBeTruthy();
    }
    expect(TASK_STATE_META.running.active).toBe(true);
    expect(TASK_STATE_META.done.active).toBe(false);
  });
});
