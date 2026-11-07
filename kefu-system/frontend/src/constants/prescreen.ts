/**
 * 预筛正则内置预设（共享约定：与后端 analyzer.prescreen 使用同一套 re 语法）。
 *
 * 存储格式约定：勾选的预设串 + 自定义正则以 `|` 合并后存入
 * settings 键 `analysis.prescreen_regex`（后端不改，命中任一即送 AI 分析）。
 * 回显时用 ``parsePrescreen`` 按预设整串 includes 检测勾选态；
 * 合并/拆分全部以前端 state 为准（selectedPresets + customRegex）。
 */

/** 配件 OE 号（覆盖丰田 5-5 / 本田 / 日产 / 福特 / 大众 / 宝马 / 奔驰 / VIN 17 位 / 小写 / 中文混排） */
export const PRESCREEN_PRESET_OE =
  String.raw`(?<![A-Za-z0-9])(?:\d{4,5}-[A-Z0-9]{3,7}(?:-[A-Z0-9]{2,4})?|[A-Z0-9]{4}-\d{7}-[A-Za-z]|[A-Z]{1,3}[- ]?\d{2,3}[- ]?\d{3}[- ]?\d{1,3}(?:[- ]?\d{1,3})?|\d{10,13}|(?i:(?=[A-Z0-9]*[A-Z])[A-Z0-9]{8,15}))(?![A-Za-z0-9])`

/** 汽车 VIN 码（17 位，排除 I/O/Q） */
export const PRESCREEN_PRESET_VIN =
  String.raw`(?<![A-Za-z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])`

/** 内置预设清单（设置页芯片渲染顺序） */
export const PRESCREEN_PRESETS: ReadonlyArray<{
  key: string
  label: string
  regex: string
}> = [
  { key: 'oe', label: '配件 OE 号', regex: PRESCREEN_PRESET_OE },
  { key: 'vin', label: '汽车 VIN 码', regex: PRESCREEN_PRESET_VIN },
]

/** 预筛正则回显解析结果：勾选的预设串集合 + 剩余自定义正则 */
export interface PrescreenState {
  selectedPresets: string[]
  customRegex: string
}

/**
 * 从已保存的合并正则串解析出勾选态与自定义部分。
 * 预设串本身含 `|`，无法按 `|` 切分，故按整串 includes 检测并剔除。
 */
export function parsePrescreen(saved: string): PrescreenState {
  let rest = String(saved ?? '')
  const selectedPresets: string[] = []
  for (const preset of PRESCREEN_PRESETS) {
    if (rest.includes(preset.regex)) {
      selectedPresets.push(preset.regex)
      rest = rest.split(preset.regex).join('')
    }
  }
  // 剔除预设后清理残留分隔符，剩余片段即自定义正则
  const customRegex = rest
    .split('|')
    .map((s) => s.trim())
    .filter(Boolean)
    .join('|')
  return { selectedPresets, customRegex }
}

/** 勾选的预设 + 自定义正则合并为一个存储串（空片段自动过滤）。 */
export function mergePrescreen(
  selectedPresets: string[],
  customRegex: string,
): string {
  return [...selectedPresets, String(customRegex ?? '').trim()]
    .filter(Boolean)
    .join('|')
}
