import { FormControlLabel, Switch as MuiSwitch, type SwitchProps as MuiSwitchProps } from "@mui/material";
import type { ReactNode } from "react";

export interface SwitchProps extends Omit<MuiSwitchProps, "size"> {
  /** 带标签时渲染成「行内标签 + 开关」；无标签时就是裸开关（放 InsetRow.control 用） */
  label?: ReactNode;
}

/** iOS 风格开关（外观在 theme MuiSwitch 覆盖里定制）。
 * 带标签时是一整行：文案最左、开关最右（缺陷清单「开关组件应该在最右侧，文案在最左侧」）。 */
export function Switch({ label, ...rest }: SwitchProps) {
  if (label === undefined) return <MuiSwitch {...rest} />;
  return (
    <FormControlLabel
      control={<MuiSwitch {...rest} />}
      label={label}
      labelPlacement="start"
      sx={{ ml: 0, mr: 0, width: "100%", display: "flex", justifyContent: "space-between", gap: 2 }}
    />
  );
}
