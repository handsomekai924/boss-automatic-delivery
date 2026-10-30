import { useState, type ReactNode } from "react";
import { Box, Typography } from "@mui/material";
import {
  Button, Cascader, DataTable, EmptyState, InlineAlert, InsetGroup, InsetRow, JobCard,
  MultiSelect, PageHeader, Pill, ScoreRing, SearchFilterBar, SegmentedControl, Select,
  Sheet, Skeleton, StatusDot, Surface, Switch, TaskPanel, TextField, useToast,
} from "../../components";
import { useThemeMode } from "../../theme/ThemeModeProvider";

/**
 * M0 组件画廊：设计系统与公用组件的可视化验收页（apple-design「交互原型胜过百万张静态图」）。
 * M1 换成真壳后此页保留在 /dev/gallery 供回归走查。
 */
export function GalleryPage() {
  const { mode, toggle } = useThemeMode();
  const { toast } = useToast();
  const [platform, setPlatform] = useState("boss");
  const [switchOn, setSwitchOn] = useState(true);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [industries, setIndustries] = useState<string[]>([]);
  const [city, setCity] = useState("");
  const [level, setLevel] = useState("");

  return (
    <Box sx={{ maxWidth: 960, mx: "auto", px: 3, py: 5 }}>
      <PageHeader
        title="组件画廊"
        subtitle="Apple 设计系统 · M0 公用组件验收（docs/DESIGN.md）"
        actions={
          <>
            <Button variant="gray" onClick={toggle}>
              切换到{mode === "dark" ? "浅色" : "深色"}
            </Button>
            <Button variant="filled" onClick={() => toast("已保存", "ok")}>
              弹 Toast
            </Button>
          </>
        }
      />

      <Section title="按钮">
        <Box sx={{ display: "flex", gap: 1.5, flexWrap: "wrap" }}>
          <Button variant="filled">主操作</Button>
          <Button variant="gray">次操作</Button>
          <Button variant="tinted">弱调</Button>
          <Button variant="plain">纯文字</Button>
          <Button variant="danger">删除</Button>
          <Button variant="filled" disabled>禁用</Button>
        </Box>
      </Section>

      <Section title="分段控件（平台切换器）">
        <SegmentedControl
          aria-label="平台切换"
          value={platform}
          onChange={setPlatform}
          options={[
            { value: "boss", label: "BOSS直聘", status: "ok" },
            { value: "job51", label: "前程无忧", status: "warn" },
            { value: "lagou", label: "拉勾" },
          ]}
        />
      </Section>

      <Section title="状态与标签">
        <Box sx={{ display: "flex", gap: 2.5, flexWrap: "wrap", alignItems: "center" }}>
          <StatusDot tone="ok" label="已登录" />
          <StatusDot tone="busy" label="登录进行中" />
          <StatusDot tone="warn" label="未登录" />
          <StatusDot tone="danger" label="风控拦截" />
        </Box>
        <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", mt: 2 }}>
          <Pill tone="static">待命</Pill>
          <Pill tone="accent">进行中</Pill>
          <Pill tone="ok">已完成</Pill>
          <Pill tone="warn">待回复</Pill>
          <Pill tone="danger">失败</Pill>
        </Box>
      </Section>

      <Section title="卡片与分组列表">
        <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 2.5 }}>
          <Surface>
            <Typography variant="h3" sx={{ mb: 1 }}>实底卡片</Typography>
            <Typography variant="body1" sx={{ color: "text.secondary" }}>
              卡片永远实底；玻璃只在顶栏 / 侧栏 / 浮层。
            </Typography>
            <Box sx={{ display: "flex", gap: 2, alignItems: "center", mt: 2.5 }}>
              <ScoreRing score={87} label="匹配" />
              <ScoreRing score={64} label="匹配" />
              <ScoreRing score={41} label="匹配" />
            </Box>
          </Surface>
          <Surface padded={false}>
            <Box sx={{ p: 2.5 }}>
              <Typography variant="h3">加载骨架</Typography>
              <Skeleton variant="text" width="70%" sx={{ mt: 1.5 }} />
              <Skeleton variant="text" width="45%" />
              <Skeleton variant="rectangular" height={72} sx={{ mt: 1.5 }} />
            </Box>
          </Surface>
        </Box>

        <InsetGroup header="平台设置" footer="节流与配额是站点属性，不是全局常量。">
          <InsetRow label="登录状态" detail="138****8000" control={<StatusDot tone="ok" label="已登录" />} />
          <InsetRow label="发完对话立即投递" control={<Switch checked={switchOn} onChange={(_, v) => setSwitchOn(v)} />} />
          <InsetRow label="每日投递配额" detail="30 / 50" onClick={() => toast("打开配额设置", "info")} />
          <InsetRow label="危险操作" detail="不可用示例" disabled />
        </InsetGroup>
      </Section>

      <Section title="反馈">
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1.25 }}>
          <InlineAlert tone="ok">投递完成：成功 8 · 跳过 2</InlineAlert>
          <InlineAlert tone="warn">今日配额还剩 5 条，达到上限后自动停止。</InlineAlert>
          <InlineAlert tone="danger">连不上本地程序，请确认服务窗口还开着。</InlineAlert>
          <InlineAlert tone="info">对方回复后才能投递简历（默认门槛）。</InlineAlert>
        </Box>
        <Box sx={{ display: "flex", gap: 1.5, mt: 2, flexWrap: "wrap" }}>
          <Button variant="gray" onClick={() => toast("任务已取消", "warn")}>警告 Toast</Button>
          <Button variant="gray" onClick={() => toast("网络不通", "danger")}>错误 Toast</Button>
          <Button variant="gray" onClick={() => setSheetOpen(true)}>打开 Sheet</Button>
        </Box>
      </Section>

      <Section title="空态">
        <Surface padded={false}>
          <EmptyState
            title="还没有抓取记录"
            description="去「职位」页抓取第一批职位，这里会显示历史。"
            action={<Button variant="filled">去抓取</Button>}
          />
        </Surface>
      </Section>

      <Section title="表单与筛选">
        <SearchFilterBar
          query={query}
          onQueryChange={setQuery}
          placeholder="搜索职位 / 公司…"
          filters={[
            { value: "all", label: "全部" },
            { value: "unread", label: "未读" },
            { value: "hot", label: "热招" },
          ]}
          activeFilter={filter}
          onFilterChange={setFilter}
          actions={<Button variant="gray" size="small">重置</Button>}
        />
        <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap", mt: 2.5, alignItems: "flex-end" }}>
          <TextField label="关键词" placeholder="如：前端" value={query} onChange={(e) => setQuery(e.target.value)} />
          <Select
            label="学历"
            value={level}
            onChange={setLevel}
            options={[{ code: "bachelor", name: "本科" }, { code: "master", name: "硕士" }]}
          />
          <MultiSelect
            label="行业"
            options={[
              { code: "it", name: "互联网" },
              { code: "fin", name: "金融" },
              { code: "edu", name: "教育" },
            ]}
            values={industries}
            onChange={setIndustries}
          />
          <Cascader
            label="城市"
            nodes={[
              { code: "gd", name: "广东", children: [{ code: "gz", name: "广州" }, { code: "sz", name: "深圳" }] },
              { code: "zj", name: "浙江", children: [{ code: "hz", name: "杭州" }] },
            ]}
            value={city}
            onChange={(v) => setCity(String(v))}
          />
        </Box>
      </Section>

      <Section title="数据表（职位）">
        <Surface padded={false}>
          <DataTable
            columns={[
              { key: "job_name", header: "职位" },
              { key: "company", header: "公司", hideOnNarrow: true },
              { key: "city", header: "城市" },
              { key: "salary", header: "薪资", align: "right" },
            ]}
            rows={[
              { id: "1", job_name: "前端工程师", company: "示例科技", city: "广州", salary: "20-30K" },
              { id: "2", job_name: "产品经理", company: "样例网络", city: "深圳", salary: "25-35K" },
            ]}
            rowKey={(r) => r.id}
            onRowClick={() => toast("打开职位详情", "info")}
          />
        </Surface>
        <Box sx={{ mt: 2, display: "flex", flexDirection: "column", gap: 1.5 }}>
          <JobCard
            job={{
              job_id: "j1",
              job_name: "高级前端工程师",
              brand_name: "示例科技",
              salary_desc: "25-40K · 14 薪",
              location: "广州 · 天河区",
              job_experience: "3-5 年",
              job_degree: "本科",
              score: 87,
              jd_status: "completed",
            }}
            onClick={() => toast("打开 JobCard", "info")}
          />
        </Box>
      </Section>

      <Section title="长任务面板">
        <TaskPanel
          title="批量匹配"
          status="running"
          percent={40}
          current="某职位 · 高级前端"
          counts={[["完成", "12 / 30"], ["命中", "8"]]}
          eta={88}
          eventsTitle="事件流"
          events={[
            { name: "开始匹配 30 条", at: 1700000000, kind: "info" },
            { name: "命中规则：公司黑名单检查通过", at: 1700000030, kind: "ok" },
            { name: "第 12 条 JD 缺失，跳过", at: 1700000060, kind: "warn" },
          ]}
          onStop={() => toast("已停止", "warn")}
        />
      </Section>

      <Sheet open={sheetOpen} onClose={() => setSheetOpen(false)} title="Sheet 示例" anchor="right">
        <Typography variant="body1">
          材质化进入（blur + scale），dim 聚焦；右侧滑入、回右侧（同路进出）。
          动画可打断：reduced-motion 自动降级为淡入。
        </Typography>
        <Box sx={{ mt: 3, display: "flex", gap: 1.5 }}>
          <Button variant="filled" onClick={() => setSheetOpen(false)}>知道了</Button>
        </Box>
      </Sheet>
    </Box>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Box sx={{ mb: 4.5 }}>
      <Typography variant="h3" sx={{ mb: 2 }}>{title}</Typography>
      {children}
    </Box>
  );
}
