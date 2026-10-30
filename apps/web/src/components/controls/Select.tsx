import { useCallback, useRef } from "react";
import { Box, MenuItem, TextField, type TextFieldProps, type SelectProps as MuiSelectProps } from "@mui/material";
import type { SelectOption } from "./MultiSelect";

export interface SelectProps extends Omit<TextFieldProps, "select" | "onChange" | "value"> {
  options: SelectOption[];
  value: string;
  onChange: (value: string) => void;
  /** 「不限」空值项文案；null 则不加 */
  emptyLabel?: string | null;
}

/**
 * 单选下拉（表单散件）：复用 TextField 骨架 + 弹层材质走 MuiMenu theme。
 *
 * 两条体验硬约束（缺陷清单「下拉框 / 批次选择下拉框」）：
 * - 选中即收起（exit 动画 0ms）并立刻回调，不等动画走完才执行请求；
 * - 鼠标收起（点选项 / 点外面）后不把焦点留在触发器上，否则 MUI 的
 *   restore-focus 会把聚焦蓝框又带回来。键盘收起（Esc / 键盘选中）保留焦点。
 */
export function Select({ options, value, onChange, emptyLabel = "不限", slotProps, ...rest }: SelectProps) {
  const rootRef = useRef<HTMLDivElement>(null);

  const handleClose = useCallback(
    (event: React.SyntheticEvent) => {
      const display = rootRef.current?.querySelector<HTMLElement>('[role="combobox"]');
      if (event.type === "keydown") display?.focus();
      else display?.blur();
    },
    [],
  );

  // TextField 的 select 槽是 SelectInput：收口 Menu 行为（不等收起动画、不回填焦点）
  const selectSlot: Partial<MuiSelectProps> = {
    onClose: handleClose,
    MenuProps: {
      transitionDuration: { appear: 120, enter: 120, exit: 0 },
      slotProps: { root: { disableRestoreFocus: true } },
    },
  };

  return (
    <Box ref={rootRef} sx={{ display: "contents" }}>
      <TextField
        select
        {...rest}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        slotProps={{
          ...slotProps,
          select: { ...(slotProps?.select as Partial<MuiSelectProps> | undefined), ...selectSlot },
        } as TextFieldProps["slotProps"]}
      >
        {emptyLabel !== null && <MenuItem value="">{emptyLabel}</MenuItem>}
        {options.map((o) => (
          <MenuItem key={o.code} value={o.code}>{o.name}</MenuItem>
        ))}
      </TextField>
    </Box>
  );
}
