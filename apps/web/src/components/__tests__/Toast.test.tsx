import { describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeModeProvider } from "../../theme/ThemeModeProvider";
import { ToastProvider, useToast } from "../feedback/Toast";
import { Button } from "../controls/Button";

function Harness() {
  const { toast } = useToast();
  return <Button onClick={() => toast("已保存", "ok", 150)}>弹</Button>;
}

describe("Toast", () => {
  it("触发后显示消息并自动消失", async () => {
    render(
      <ThemeModeProvider>
        <ToastProvider>
          <Harness />
        </ToastProvider>
      </ThemeModeProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "弹" }));
    expect(screen.getByText("已保存")).toBeInTheDocument();

    await waitFor(
      () => {
        expect(screen.queryByText("已保存")).not.toBeInTheDocument();
      },
      { timeout: 1500 },
    );
  });
});
