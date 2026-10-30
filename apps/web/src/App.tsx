/**
 * 应用入口壳（docs 5.1）：Hash 路由 + 平台上下文 + 一条 WS 连接 + 两分支路由表。
 *
 * `PlatformProvider` 要吃路由参数，所以必须在 Router 里面；
 * `WsBridge` 只连 `/api/ws` 落 store，挂一次全局够用。
 */

import { HashRouter } from "react-router-dom";

import { PlatformProvider } from "./platform/PlatformProvider";
import { AppRoutes } from "./routes";
import { WsBridge } from "./ws/WsBridge";

export function App() {
  return (
    <HashRouter>
      <PlatformProvider>
        <WsBridge />
        <AppRoutes />
      </PlatformProvider>
    </HashRouter>
  );
}
