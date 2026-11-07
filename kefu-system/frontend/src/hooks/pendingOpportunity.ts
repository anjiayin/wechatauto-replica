/**
 * 「点击新商机 toast → 打开详情抽屉」的跨页挂载竞态兜底。
 *
 * App 派发 kefu-open-opportunity 时若商机列表页尚未挂载（当前在
 * 设置/消息页），监听器未注册、事件会丢失。这里用模块级 pending 变量
 * 暂存 opportunity_id，列表页挂载时检查并消费：
 * - 列表页已挂载：事件处理器直接打开抽屉，并同步清空 pending（防陈旧）；
 * - 列表页未挂载：pending 保留，挂载后消费打开抽屉。
 */
let pendingOpportunityId: number | null = null

/** 记录待打开的商机 id（App 点击 toast 时调用）。 */
export function setPendingOpportunity(id: number): void {
  pendingOpportunityId = id
}

/** 取出并清空待打开的商机 id（列表页挂载时调用，仅消费一次）。 */
export function consumePendingOpportunity(): number | null {
  const id = pendingOpportunityId
  pendingOpportunityId = null
  return id
}
