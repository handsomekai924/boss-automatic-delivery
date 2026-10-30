/** 公用组件库出口（docs/multiplatform-console-redesign.md 5.3 的第一批）。 */

export { Button, type ButtonProps, type ButtonVariant } from "./controls/Button";
export {
  SegmentedControl, type SegmentedControlProps, type SegmentedOption,
} from "./controls/SegmentedControl";
export { Switch, type SwitchProps } from "./controls/Switch";
export { MultiSelect, type MultiSelectProps, type SelectOption } from "./controls/MultiSelect";
export { Cascader, type CascaderProps, type CascaderNode } from "./controls/Cascader";
export { TextField, type TextFieldProps } from "./controls/TextField";
export { Select, type SelectProps } from "./controls/Select";

export { StatusDot, type StatusDotProps, type StatusTone } from "./data/StatusDot";
export { Pill, type PillProps, type PillTone } from "./data/Pill";
export { EmptyState, type EmptyStateProps } from "./data/EmptyState";
export { Skeleton } from "./data/Skeleton";
export { ScoreRing, type ScoreRingProps } from "./data/ScoreRing";
export { DataTable, CellTitle, type Column, type DataTableProps } from "./data/DataTable";
export { SearchFilterBar, type SearchFilterBarProps, type FilterOption } from "./data/SearchFilterBar";
export { JobCard, type JobCardProps } from "./data/JobCard";
export { JobDetailDrawer, type JobDetailDrawerProps } from "./data/JobDetailDrawer";
export { StatTile, type StatTileProps } from "./data/StatTile";
export { ChartCard, type ChartCardProps, type ChartTable } from "./data/ChartCard";
export { BarChart, type BarChartProps, type BarDatum } from "./data/BarChart";
export { ColumnChart, type ColumnChartProps, type ColumnDatum } from "./data/ColumnChart";

export { Surface, type SurfaceProps } from "./surfaces/Surface";
export { InsetGroup, InsetRow, type InsetGroupProps, type InsetRowProps } from "./surfaces/InsetGroup";

export { ToastProvider, useToast, type ToastApi, type ToastTone } from "./feedback/Toast";
export { Sheet, type SheetProps } from "./feedback/Sheet";
export { ConfirmSheet, type ConfirmSheetProps } from "./feedback/ConfirmSheet";
export { InlineAlert, type InlineAlertProps, type InlineAlertTone } from "./feedback/InlineAlert";
export { ProgressBar, type ProgressBarProps } from "./feedback/ProgressBar";
export { EventStream, type EventStreamProps, type TaskEvent, type EventKind } from "./feedback/EventStream";
export { TaskPanel, type TaskPanelProps } from "./feedback/TaskPanel";

export { PageHeader, type PageHeaderProps } from "./layout/PageHeader";
export { TopBar, type TopBarProps } from "./layout/TopBar";
export { PlatformSidebar, type PlatformSidebarProps } from "./layout/PlatformSidebar";
export { TaskBadge, SessionSignal } from "./layout/TaskBadge";
export { PageTransition } from "./layout/PageTransition";
export { ViewFade } from "./layout/ViewFade";
