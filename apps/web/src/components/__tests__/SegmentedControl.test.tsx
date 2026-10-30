import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { ThemeModeProvider } from "../../theme/ThemeModeProvider";
import { SegmentedControl } from "../controls/SegmentedControl";

function Harness({ onChange }: { onChange: (v: string) => void }) {
  const [value, setValue] = useState("boss");
  return (
    <SegmentedControl
      aria-label="平台切换"
      value={value}
      onChange={(v) => {
        setValue(v);
        onChange(v);
      }}
      options={[
        { value: "boss", label: "BOSS直聘" },
        { value: "job51", label: "前程无忧" },
      ]}
    />
  );
}

describe("SegmentedControl", () => {
  it("点击切换选中项并回调", async () => {
    const onChange = vi.fn();
    render(
      <ThemeModeProvider>
        <Harness onChange={onChange} />
      </ThemeModeProvider>,
    );
    await userEvent.click(screen.getByRole("radio", { name: /前程无忧/ }));
    expect(onChange).toHaveBeenCalledWith("job51");
    expect(screen.getByRole("radio", { name: /前程无忧/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /BOSS直聘/ })).toHaveAttribute("aria-checked", "false");
  });

  it("方向键在选项间移动", async () => {
    const onChange = vi.fn();
    render(
      <ThemeModeProvider>
        <Harness onChange={onChange} />
      </ThemeModeProvider>,
    );
    screen.getByRole("radio", { name: /BOSS直聘/ }).focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(onChange).toHaveBeenCalledWith("job51");
  });
});
