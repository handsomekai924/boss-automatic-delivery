/**
 * 未迁移页面的占位（拍板 #5「legacy 共存逐页切」）。
 *
 * 新壳先把布局/路由/上下文跑通，这页给出通往旧壳对应视图的直达入口；
 * 逐页迁完就删掉这条占位路由，不留死链。
 */

import { Box, Typography } from "@mui/material";
import { Button, EmptyState, PageHeader, Surface } from "../components";
import { fontFamilyMono, typeScale } from "../theme/tokens";

export interface PendingPageProps {
  title: string;
  /** 旧壳 hash 路径（如 `#/resume`）；不给就只是占位，不出直达按钮 */
  legacyHash?: string;
  /** 这页归谁：平台显示名或「共用」 */
  scope: string;
}

/** 把旧 hash 拼成 `/legacy/#/…` 的完整入口。 */
export function legacyHref(hash: string): string {
  return `/legacy/#${hash.startsWith("/") ? hash : `/${hash}`}`;
}

export function PendingPage({ title, legacyHash, scope }: PendingPageProps) {
  return (
    <Box>
      <PageHeader title={title} subtitle={`${scope} · 还在旧壳里跑`} />
      <Surface sx={{ maxWidth: 560 }}>
        <EmptyState
          icon="◷"
          title="这一页还没迁到新壳"
          description={
            legacyHash
              ? "新壳的路由、平台切换与实时推送已经就位；这一页的功能暂时还在旧界面，点下面的按钮直接过去。"
              : "这一页要等后面的迁移阶段补上。"
          }
          action={
            legacyHash ? (
              <Button
                variant="filled"
                onClick={() => {
                  window.location.href = legacyHref(legacyHash);
                }}
              >
                打开旧界面
              </Button>
            ) : undefined
          }
        />
      </Surface>
      {legacyHash && (
        <Typography sx={{ ...typeScale.s, color: "text.secondary", mt: 2 }}>
          也可以直接访问{" "}
          <Box component="span" sx={{ fontFamily: fontFamilyMono }}>{legacyHref(legacyHash)}</Box>
          ；旧书签会自动跳到新路由。
        </Typography>
      )}
    </Box>
  );
}
