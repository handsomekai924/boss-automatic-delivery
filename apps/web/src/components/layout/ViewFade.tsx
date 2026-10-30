import type { ReactNode } from "react";
import { motion, useReducedMotion } from "motion/react";

import { springs } from "../../theme/tokens";

/**
 * 组件级过渡（缺陷「组件切换生硬」）：`slotKey` 变化时淡入 + 轻微上浮。
 * 与 PageTransition 同一手法——只做「进」，不排队出场，切换即响应；
 * reduced-motion 退化成短淡入。
 */
export function ViewFade({ slotKey, children }: { slotKey: string; children: ReactNode }) {
  const reduced = useReducedMotion();

  return (
    <motion.div
      key={slotKey}
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={reduced ? { duration: 0.15 } : springs.ui}
    >
      {children}
    </motion.div>
  );
}
