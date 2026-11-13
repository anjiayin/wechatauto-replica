/**
 * 批次 11 需求 1 —— 前端「图片行 ↔ raw_refs」匹配逻辑独立验证（QA 严过关）。
 *
 * 说明：匹配逻辑内联在 `OpportunityDetail.tsx` 的 useMemo 里（未导出），
 * 无法 import。本文件把那段逻辑**逐字符复制**过来，用真实数据结构跑用例。
 * 复制源：frontend/src/pages/OpportunityDetail.tsx:299-316
 *
 * 运行：node tests/frontend/rawrefs_match_check.mjs
 */
import assert from 'node:assert/strict'

/** 复制自 OpportunityDetail.tsx（rawImageIds 的 useMemo 实现） */
function computeRawImageIds(rawTextDisplay, rawText, refsIn) {
  const rawLines = (rawTextDisplay || '').split('\n')
  const rawSourceLines = (rawText || '').split('\n')
  const refs = refsIn ?? []
  const ids = rawLines.map(() => null)
  let cursor = 0
  for (let idx = 0; idx < rawLines.length && cursor < refs.length; idx += 1) {
    const ref = refs[cursor]
    if (!ref || typeof ref.line !== 'string') break
    if (rawLines[idx] !== ref.line && rawSourceLines[idx] !== ref.line) continue
    const localId = Number(ref.local_id)
    if (Number.isFinite(localId) && localId > 0) ids[idx] = localId
    cursor += 1
  }
  return ids
}

const results = []
function check(name, fn) {
  try {
    fn()
    results.push(`PASS  ${name}`)
  } catch (err) {
    results.push(`FAIL  ${name}\n        ${err.message.split('\n').join('\n        ')}`)
  }
}

// ---------------------------------------------------------------------------
// 用例 1：群聊，raw_text_display 把 wxid 换成了群昵称
//   （历史数据/pre-批次5 数据才有 wxid 前缀；伪造最坏情况）
// ---------------------------------------------------------------------------
check('群聊：显示名替换后仍能命中（靠 raw_text 原行回落）', () => {
  const rawText = [
    '[10:00] wxid_a: 有没有前保险杠',
    '[10:01] wxid_a: [图片] 识别：车辆/汽车配件（前保险杠）',
    '[10:02] wxid_a: 有货联系我',
  ].join('\n')
  const display = [
    '[10:00] 张三: 有没有前保险杠',
    '[10:01] 张三: [图片] 识别：车辆/汽车配件（前保险杠）',
    '[10:02] 张三: 有货联系我',
  ].join('\n')
  const refs = [
    { line: '[10:01] wxid_a: [图片] 识别：车辆/汽车配件（前保险杠）', local_id: 131, kind: 'image' },
  ]
  const ids = computeRawImageIds(display, rawText, refs)
  assert.deepEqual(ids, [null, 131, null])
})

// ---------------------------------------------------------------------------
// 用例 2：完全相同文本出现多次（真实库里商机 #130 就有 8 行重复）
//   两个 ref 的 local_id 不同 → 必须按顺序各对一行、不重复渲染同一张图
// ---------------------------------------------------------------------------
check('重复文本：顺序消费指针一一对应，不重复命中', () => {
  const same = '[图片] 识别：车辆/汽车配件'
  const rawText = `[10:00] ${same}\n[10:01] ${same}\n[10:02] 别的`
  const refs = [
    { line: `[10:00] ${same}`, local_id: 11, kind: 'image' },
    { line: `[10:01] ${same}`, local_id: 12, kind: 'image' },
  ]
  const ids = computeRawImageIds(rawText, rawText, refs)
  assert.deepEqual(ids, [11, 12, null])
})

// ---------------------------------------------------------------------------
// 用例 3：multiline 正文（真实库商机 #132 一行消息里含 9 个换行）
//   raw_text 行数 > unit.lines 数，ref 必须仍能落到正确的那一行
// ---------------------------------------------------------------------------
check('多行正文：行号错位也能正确命中（按文本等值滑窗）', () => {
  const rawText = [
    '[10:37] 19款几何A',
    '主驾驶座椅 黑皮手动调节',
    '[10:38] [图片] 识别：车辆/汽车配件（前大灯）',
    '[10:39] 补充说明',
  ].join('\n')
  const refs = [{ line: '[10:38] [图片] 识别：车辆/汽车配件（前大灯）', local_id: 88, kind: 'image' }]
  const ids = computeRawImageIds(rawText, rawText, refs)
  assert.deepEqual(ids, [null, null, 88, null])
})

// ---------------------------------------------------------------------------
// 用例 4（重点失败点）：图片行文本含换行符
//   part_name 由视觉模型返回，服务端只做了 .strip()[:40]，内部换行会保留；
//   此时 ref.line 含 '\n'，与 split('\n') 后的单行永不相等 → 图片静默丢失
// ---------------------------------------------------------------------------
check('图片行含换行 → 漏图（已知缺陷，见报告 M 级）', () => {
  const refLine = '[10:01] [图片] 识别：车辆/汽车配件（前保险杠\n（左））'
  const rawText = `[10:00] 有货吗\n${refLine}`
  const refs = [{ line: refLine, local_id: 131, kind: 'image' }]
  const ids = computeRawImageIds(rawText, rawText, refs)
  assert.deepEqual(ids, [null, null, null], '当前实现会漏图')
})

// ---------------------------------------------------------------------------
// 用例 5：行文本被 trim/多余空格改动（手工改库、旧版本写入）
// ---------------------------------------------------------------------------
check('行尾空格不一致 → 漏图（等值匹配的固有风险）', () => {
  const rawText = '[10:00] 有货吗\n[10:01] [图片] 识别：车辆/汽车配件 '
  const refs = [{ line: '[10:01] [图片] 识别：车辆/汽车配件', local_id: 7, kind: 'image' }]
  const ids = computeRawImageIds(rawText, rawText, refs)
  assert.deepEqual(ids, [null, null])
})

// ---------------------------------------------------------------------------
// 用例 6：refs 顺序与原文顺序不一致（理论上不会发生，验证不会错挂）
// ---------------------------------------------------------------------------
check('refs 顺序颠倒时不会错挂到错误行（宁可漏也不挂错）', () => {
  const rawText = '[10:00] A\n[10:01] B'
  const refs = [
    { line: '[10:01] B', local_id: 2, kind: 'image' },
    { line: '[10:00] A', local_id: 1, kind: 'image' },
  ]
  const ids = computeRawImageIds(rawText, rawText, refs)
  assert.deepEqual(ids, [null, 2])
})

// ---------------------------------------------------------------------------
// 用例 7：脏数据（line 非字符串）→ 前端守卫 break，不抛异常
// ---------------------------------------------------------------------------
check('脏 raw_refs（line 非字符串）不抛异常', () => {
  const rawText = '[10:00] A\n[10:01] B'
  const ids = computeRawImageIds(rawText, rawText, [{ line: 1, local_id: 5 }])
  assert.deepEqual(ids, [null, null])
})

// ---------------------------------------------------------------------------
// 用例 8：合并后 raw_text 追加新批次（顺序必须保持）
// ---------------------------------------------------------------------------
check('合并追加：老 ref 在前、新 ref 在后，各自命中', () => {
  const rawText = [
    '[10:00] 老一句',
    '[10:01] [图片] 识别：车辆/汽车配件（大灯）',
    '[10:10] 新一句',
    '[10:11] [图片] 识别：车辆/汽车配件（叶子板）',
  ].join('\n')
  const refs = [
    { line: '[10:01] [图片] 识别：车辆/汽车配件（大灯）', local_id: 201, kind: 'image' },
    { line: '[10:11] [图片] 识别：车辆/汽车配件（叶子板）', local_id: 202, kind: 'image' },
  ]
  assert.deepEqual(computeRawImageIds(rawText, rawText, refs), [null, 201, null, 202])
})

console.log(results.join('\n'))
const failed = results.filter((r) => r.startsWith('FAIL')).length
console.log(`\n合计 ${results.length} 项，失败 ${failed} 项`)
// 用例 4/5 是「已知缺陷」用例：断言的是当前行为（漏图），因此全部应为 PASS
process.exit(failed === 0 ? 0 : 1)
