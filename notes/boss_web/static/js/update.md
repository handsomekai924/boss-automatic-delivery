# `boss_web.static.js.update` 协议笔记

> 摘自 `boss_web/static/js/update.js` 的 JSDoc / 注释。

## 一句话

版本更新提示：首屏弹窗 + 挂机 toast + 应用内自更新进度；`showUpdateDone` 只刷新不关窗。

## 细节

### `showUpdateDone`

更新完成：**只刷新页面**，绝不关标签页。

这里踩过三个坑，改回去之前先看清楚：

- **绝不 `window.close()`**。本应用的标签页是用户自己开出来的、没有历史记录
  （`history.length === 1`），Chrome / Edge / Firefox 对这种情况**照关不误**
  ——`try/catch` 也兜不住（被拦时只打一条 console 警告，不抛异常）。旧版本
  一句 `window.close()` 把页面直接干掉，而重启的 exe 带 `--no-browser`，
  不会自己再开一个，用户手上就一个页面都不剩了。刷新就没有这个问题。
- **刷之前必须记住「这页处理过」**。回执在服务端是**进程内缓存**的
  （见 `services/updater.py` 的 `consume_done_marker`），同一个进程会一直回同一份；
  不记标记就会「看到回执 → 刷新 → 又看到回执 → 再刷新」无限刷。
  标记跟着标签页活过刷新（`sessionStorage`），所以每个标签页最多刷一次。
- **每个标签页都刷**（不只是发起更新的那个）。更新后重新打开的页面虽然本来就是
  新服务发的，但这里不做区分：拿到回执就统一刷一遍，保证手上这页一定不跑旧代码。

### `showUpdateModal` 的「立即更新」时序

- 一开跑就 `watchForNewService` 盯 `update_done`：swap 期 WS 必断，进度接不上但完成回执能接上。
- 先挂 WS 再发 `apply`：下载一开跑就有百分比，不会错过开头。
- swap 期 `onDown` 断开是预期的，只换一句提示；完成靠轮询接手。
- 忽略成功 = 这一版不再提醒，圆点**立刻**熄掉（别等下一轮轮询才灭）。
