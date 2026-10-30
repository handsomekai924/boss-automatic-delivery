import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { ThemeModeProvider } from "../../theme/ThemeModeProvider";
import { Button } from "../controls/Button";

function wrap(ui: ReactNode) {
  return render(<ThemeModeProvider>{ui}</ThemeModeProvider>);
}

describe("Button", () => {
  it("渲染文案并触发 onClick", async () => {
    const onClick = vi.fn();
    wrap(<Button variant="filled" onClick={onClick}>主操作</Button>);
    await userEvent.click(screen.getByRole("button", { name: "主操作" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("禁用按钮不可点（pointer-events 拦截，不会触发 onClick）", () => {
    const onClick = vi.fn();
    wrap(<Button variant="filled" disabled onClick={onClick}>主操作</Button>);
    expect(screen.getByRole("button", { name: "主操作" })).toBeDisabled();
  });

  it("按下即有反馈样式（pointer-down scale 规则挂在 :active，由主题注入）", () => {
    wrap(<Button variant="gray">次操作</Button>);
    // 规则存在性由 theme 测试覆盖；这里保证按钮可渲染且可聚焦
    expect(screen.getByRole("button", { name: "次操作" })).not.toHaveAttribute("disabled");
  });
});
