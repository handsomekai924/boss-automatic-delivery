import type { SxProps, Theme } from "@mui/material/styles";

/**
 * 合并多段 sx（数组形态是 MUI 运行时支持的写法）。
 * 这里统一收口类型断言——TS 对递归 SxProps 数组的联合推导不稳（MUI v7 已知坑）。
 */
export function mergeSx(...parts: Array<SxProps<Theme> | undefined>): SxProps<Theme> {
  return parts.filter((p) => p !== undefined) as SxProps<Theme>;
}
