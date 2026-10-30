import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ThemeModeProvider } from "../../theme/ThemeModeProvider";
import { BarChart } from "../data/BarChart";
import { ChartCard } from "../data/ChartCard";
import { ColumnChart } from "../data/ColumnChart";
import { StatTile } from "../data/StatTile";

// jsdom 没有 canvas：ECharts init 拿不到 2d context，把 echarts 模块整组换成假件，
// 断言走 setOption 收到的 option（图元/标注/tooltip 都在 option 里）；DOM 侧
// （aria-label、表格孪生、空态）照常真渲染。
const echartsMock = vi.hoisted(() => {
  const instance = {
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
  };
  return { instance, init: vi.fn(() => instance), use: vi.fn() };
});

vi.mock("echarts/core", () => ({ init: echartsMock.init, use: echartsMock.use }));
vi.mock("echarts/charts", () => ({ BarChart: class {} }));
vi.mock("echarts/components", () => ({ GridComponent: class {}, TooltipComponent: class {} }));
vi.mock("echarts/renderers", () => ({ CanvasRenderer: class {} }));

type OptionDataItem = number | { value: number; label?: { show?: boolean } };

interface CapturedOption {
  series: Array<{
    type: string;
    data: OptionDataItem[];
    barMaxWidth: number;
    barCategoryGap: string;
    label: { show: boolean; position: string; formatter: (p: { value: number }) => string };
  }>;
  xAxis: { type: string; data: string[]; axisLabel: { formatter: (v: string, i: number) => string } };
  tooltip: { trigger: string; formatter: (p: unknown) => string };
}

/** 最近一次 setOption 收到的 option（组件每次数据/主题变化都会重铺） */
function lastOption(): CapturedOption {
  return echartsMock.instance.setOption.mock.calls.at(-1)![0] as CapturedOption;
}

beforeEach(() => {
  echartsMock.init.mockClear();
  echartsMock.instance.setOption.mockClear();
  echartsMock.instance.dispose.mockClear();
});

function wrap(ui: ReactNode) {
  return render(<ThemeModeProvider>{ui}</ThemeModeProvider>);
}

describe("StatTile", () => {
  it("标签 + 值 + 说明；meter 出同色系进度条", () => {
    wrap(<StatTile label="在库职位" value="1,284" caption="保留 1,850" meter={0.77} />);
    expect(screen.getByText("在库职位")).toBeInTheDocument();
    expect(screen.getByText("1,284")).toBeInTheDocument();
    expect(screen.getByText("保留 1,850")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
  });
});

describe("BarChart", () => {
  it("setOption 拿到系列数据：条末标注带单位、tooltip 带 hint；无数据出空态不挂图", () => {
    const { unmount } = wrap(
      <BarChart
        data={[
          { label: "广州", value: 312, hint: "在库最多" },
          { label: "深圳", value: 120 },
        ]}
        unit=" 个"
      />,
    );

    // (a) 系列数据原样喂给 ECharts；几何守规范（细条 14px + 2px 表面间隙）
    const option = lastOption();
    expect(option.series[0].type).toBe("bar");
    expect(option.series[0].data).toEqual([312, 120]);
    expect(option.series[0].barMaxWidth).toBe(14);
    expect(option.series[0].barCategoryGap).toBe("2px");

    // 条末直接标注（established pattern）：值 + 单位
    expect(option.series[0].label.formatter({ value: 312 })).toBe("312 个");

    // tooltip：值先行，hint 在场才追加
    const tip = option.tooltip.formatter({ value: 312, dataIndex: 0 });
    expect(tip).toContain("312 个");
    expect(tip).toContain("广州 · 在库最多");

    // (b) 屏读摘要：整图一个 role="img" 把系列说清
    expect(screen.getByRole("img", { name: "条形图：广州 312 个、深圳 120 个" })).toBeInTheDocument();

    unmount();
    expect(echartsMock.instance.dispose).toHaveBeenCalled();

    // (d) 空数据 → 空态，不挂空图
    wrap(<BarChart data={[]} />);
    expect(screen.getByText("暂无数据")).toBeInTheDocument();
    expect(echartsMock.init).toHaveBeenCalledTimes(1);
  });
});

describe("ColumnChart", () => {
  it("选择性标注：只给最大值柱帽开数字；tooltip 值先行带 hint", () => {
    wrap(
      <ColumnChart
        data={[
          { label: "14:02", value: 10, hint: "第 1 页 · 原始 12 · 丢弃 2" },
          { label: "14:05", value: 30, hint: "第 3 页 · 原始 55 · 丢弃 13" },
          { label: "14:09", value: 20, hint: "第 4 页 · 原始 25 · 丢弃 5" },
        ]}
        unit=" 个"
      />,
    );

    // (a) 系列数据 + 每根柱的标注开关（只标最大值，不给每根柱都写数字）
    const option = lastOption();
    expect(option.xAxis.data).toEqual(["14:02", "14:05", "14:09"]);
    const items = option.series[0].data as Array<{ value: number; label: { show: boolean } }>;
    expect(items.map((d) => d.value)).toEqual([10, 30, 20]);
    expect(items.map((d) => d.label.show)).toEqual([false, true, false]);
    expect(option.series[0].label.formatter({ value: 30 })).toBe("30"); // 柱帽只写数字不带单位

    // tooltip：值先行，hint（页/原始/丢弃/新增/更新）跟在行标签后
    const tip = option.tooltip.formatter({ value: 30, dataIndex: 1 });
    expect(tip).toContain("30 个");
    expect(tip).toContain("14:05 · 第 3 页 · 原始 55 · 丢弃 13");
    expect(tip.indexOf("30 个")).toBeLessThan(tip.indexOf("14:05"));

    // (b) 屏读摘要
    expect(screen.getByRole("img", { name: "柱状图：14:02 10 个、14:05 30 个、14:09 20 个" })).toBeInTheDocument();
  });

  it("x 标签过密时隔一个显示（>8 根柱）", () => {
    const data = Array.from({ length: 10 }, (_, i) => ({ label: `t${i}`, value: i }));
    wrap(<ColumnChart data={data} />);
    const option = lastOption();
    expect(option.xAxis.data).toHaveLength(10);
    expect(option.xAxis.axisLabel.formatter("t0", 0)).toBe("t0");
    expect(option.xAxis.axisLabel.formatter("t1", 1)).toBe("");
    expect(option.xAxis.axisLabel.formatter("t8", 8)).toBe("t8");
    // 默认仍只标最大值（t9）
    const shows = (option.series[0].data as Array<{ label: { show: boolean } }>).map((d) => d.label.show);
    expect(shows).toEqual([false, false, false, false, false, false, false, false, false, true]);
  });
});

describe("ChartCard", () => {
  it("图表/表格切换：表格孪生把数值不靠悬停也能取到", async () => {
    wrap(
      <ChartCard
        title="城市分布"
        caption="在库职位按城市计数"
        table={{ head: ["城市", "职位数"], rows: [["广州", "312"]] }}
      >
        <BarChart data={[{ label: "广州", value: 312 }]} />
      </ChartCard>,
    );
    expect(screen.getByText("城市分布")).toBeInTheDocument();

    // (c) 表格孪生是真 DOM：不悬停也能取到数值
    await userEvent.click(screen.getByRole("radio", { name: "表格" }));
    expect(screen.getByRole("columnheader", { name: "城市" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "312" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: "图表" }));
    expect(screen.queryByRole("cell", { name: "312" })).not.toBeInTheDocument();
  });
});
