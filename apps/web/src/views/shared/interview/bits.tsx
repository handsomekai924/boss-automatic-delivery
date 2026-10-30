/** 出题 / 记录两段共用的小组件（勾选 + 分页脚）。 */

import { Box, Checkbox, Typography } from "@mui/material";
import { Button } from "../../../components";
import { typeScale } from "../../../theme/tokens";
import { fmtNumber } from "../../../utils/format";

/** 行勾选（components/ 没有 Checkbox——缺口记在文件头，先用 MUI Checkbox 封一层局部小组件）。 */
export function RowCheck({
  label, checked, onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <Checkbox
      size="small"
      checked={checked}
      onChange={(e) => onChange(e.target.checked)}
      slotProps={{ input: { "aria-label": label } }}
    />
  );
}

/** 分页脚：条数 + 上一页/下一页（jobs / records 两处同款）。 */
export function Pager({
  total, page, pageCount, onPrev, onNext,
}: {
  total: number;
  page: number;
  pageCount: number;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center", mt: 1.5, gap: 1, flexWrap: "wrap", flex: "0 0 auto" }}>
      <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
        共 {fmtNumber(total)} 条 · 第 {page + 1}/{Math.max(1, pageCount)} 页
      </Typography>
      <Box sx={{ display: "flex", gap: 1 }}>
        <Button size="small" variant="gray" disabled={page <= 0} onClick={onPrev}>上一页</Button>
        <Button size="small" variant="gray" disabled={page + 1 >= pageCount} onClick={onNext}>下一页</Button>
      </Box>
    </Box>
  );
}
