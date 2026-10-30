import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeModeProvider } from "../../theme/ThemeModeProvider";
import { Cascader, type CascaderNode } from "../controls/Cascader";

const tree: CascaderNode[] = [
  {
    code: "gd",
    name: "广东",
    children: [
      { code: "gz", name: "广州" },
      { code: "sz", name: "深圳", children: [{ code: "ns", name: "南山区" }] },
    ],
  },
  { code: "zj", name: "浙江", children: [{ code: "hz", name: "杭州" }] },
];

function wrap(ui: React.ReactElement) {
  return render(<ThemeModeProvider>{ui}</ThemeModeProvider>);
}

describe("Cascader 单选（城市）", () => {
  it("顶层只展开不取值，叶子即取值", async () => {
    const onChange = vi.fn();
    wrap(<Cascader nodes={tree} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button"));
    // 点省 → 只下钻，不回调
    await userEvent.click(screen.getByRole("option", { name: /广东/ }));
    expect(onChange).not.toHaveBeenCalled();
    // 点市 → 取值
    await userEvent.click(screen.getByRole("option", { name: /广州/ }));
    expect(onChange).toHaveBeenCalledWith("gz");
  });

  it("选中值回显名字", () => {
    wrap(<Cascader nodes={tree} value="ns" onChange={() => {}} />);
    expect(screen.getByRole("button")).toHaveTextContent("南山区");
    expect(screen.getByRole("button")).not.toHaveTextContent("不限");
  });
});

describe("Cascader 多选（行业）", () => {
  it("叶子勾选进草稿，确定才提交", async () => {
    const onChange = vi.fn();
    wrap(<Cascader nodes={tree} multiple value={[]} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button"));
    await userEvent.click(screen.getByRole("option", { name: /广东/ }));
    await userEvent.click(screen.getByRole("option", { name: /广州/ }));
    expect(onChange).not.toHaveBeenCalled(); // 草稿未提交
    await userEvent.click(screen.getByRole("button", { name: "确定" }));
    expect(onChange).toHaveBeenCalledWith(["gz"]);
  });
});
