import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeModeProvider } from "../../theme/ThemeModeProvider";
import { MultiSelect } from "../controls/MultiSelect";

const options = [
  { code: "it", name: "互联网" },
  { code: "fin", name: "金融" },
  { code: "edu", name: "教育" },
];

function wrap(ui: React.ReactElement) {
  return render(<ThemeModeProvider>{ui}</ThemeModeProvider>);
}

describe("MultiSelect", () => {
  it("勾选/取消回调 values", async () => {
    const onChange = vi.fn();
    wrap(<MultiSelect options={options} values={["it"]} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button"));
    await userEvent.click(screen.getByRole("option", { name: "金融" }));
    expect(onChange).toHaveBeenCalledWith(["it", "fin"]);
  });

  it("已选 >2 项显示「等 N 项」摘要", async () => {
    wrap(<MultiSelect options={options} values={["it", "fin", "edu"]} onChange={() => {}} />);
    expect(screen.getByRole("button")).toHaveTextContent("互联网 等 3 项");
    expect(screen.getByRole("button")).not.toHaveTextContent("不限");
  });
});
