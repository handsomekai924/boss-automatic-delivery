import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeModeProvider } from "../../theme/ThemeModeProvider";
import { DataTable } from "../data/DataTable";

interface Row { id: string; name: string; city: string; }
const rows: Row[] = [
  { id: "1", name: "前端工程师", city: "广州" },
  { id: "2", name: "产品经理", city: "深圳" },
];

function wrap(ui: React.ReactElement) {
  return render(<ThemeModeProvider>{ui}</ThemeModeProvider>);
}

describe("DataTable", () => {
  it("渲染表头与行；点击行回调", async () => {
    const onRowClick = vi.fn();
    wrap(
      <DataTable
        columns={[
          { key: "name", header: "职位" },
          { key: "city", header: "城市", hideOnNarrow: true },
        ]}
        rows={rows}
        rowKey={(r) => r.id}
        onRowClick={onRowClick}
      />,
    );
    expect(screen.getByText("职位")).toBeInTheDocument();
    expect(screen.getByText("前端工程师")).toBeInTheDocument();
    await userEvent.click(screen.getByText("产品经理"));
    expect(onRowClick).toHaveBeenCalledWith(rows[1]);
  });

  it("空态与加载态", () => {
    wrap(<DataTable columns={[{ key: "name", header: "职位" }]} rows={[] as Row[]} rowKey={(r) => r.id} emptyText="没有职位" />);
    expect(screen.getByText("没有职位")).toBeInTheDocument();

    wrap(<DataTable columns={[{ key: "name", header: "职位" }]} rows={rows} rowKey={(r) => r.id} loading />);
    expect(screen.queryByText("前端工程师")).not.toBeInTheDocument();
  });
});
