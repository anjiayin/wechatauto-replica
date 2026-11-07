/**
 * 新商机提示音（Web Audio API 合成短音，不引入音频文件）。
 *
 * 浏览器自动播放策略修复：AudioContext 在用户首次与页面交互前处于
 * suspended 状态，直接播放会被拒绝且异常易被吞掉。策略：
 * - 开启提示音后立即挂载 pointerdown（once）解锁监听，首次交互恢复播放能力；
 * - 播放时若仍被挂起，resume 成功后补播；resume 失败则只保留解锁监听；
 * - 任何异常静默降级，绝不向外抛错。
 */
let sharedCtx: AudioContext | null = null
let unlockArmed = false

function getAudioContext(): AudioContext | null {
  try {
    if (!sharedCtx) {
      const Ctor: typeof AudioContext | undefined =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext
      if (!Ctor) return null
      sharedCtx = new Ctor()
    }
    return sharedCtx
  } catch {
    return null
  }
}

/** 注册「首次用户交互即解锁音频」监听（幂等，once 自动移除）。 */
export function armAudioUnlock(): void {
  if (unlockArmed) return
  unlockArmed = true
  const unlock = () => {
    unlockArmed = false
    const ctx = getAudioContext()
    if (ctx && ctx.state === 'suspended') {
      ctx.resume().catch(() => {})
    }
  }
  document.addEventListener('pointerdown', unlock, { once: true })
}

function playTone(ctx: AudioContext): void {
  const osc = ctx.createOscillator()
  const gain = ctx.createGain()
  osc.connect(gain)
  gain.connect(ctx.destination)
  osc.frequency.value = 880
  gain.gain.setValueAtTime(0.15, ctx.currentTime)
  gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5)
  osc.start()
  osc.stop(ctx.currentTime + 0.5)
}

/** 播放新商机提示音；失败静默降级（挂起时注册首次交互解锁）。 */
export function playOpportunitySound(): void {
  const ctx = getAudioContext()
  if (!ctx) return
  try {
    if (ctx.state === 'suspended') {
      armAudioUnlock()
      ctx.resume()
        .then(() => playTone(ctx))
        .catch(() => armAudioUnlock())
      return
    }
    playTone(ctx)
  } catch {
    armAudioUnlock()
  }
}
