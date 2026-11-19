import { host, useValue } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

function WindowsScreen({ rest }) {
  const visible = useValue(host.paneVisibility('windows-pc:screen'))
  const [playing, setPlaying] = useState(false)
  const [frame, setFrame] = useState(null)
  const [offline, setOffline] = useState(false)
  useEffect(() => {
    setFrame(null)
    setOffline(false)
    if (!playing || !visible) return
    let stopped = false
    let timer
    async function refresh() {
      if (!document.hidden) {
        try {
          const result = await rest('/frame', { timeoutMs: 8500 })
          if (!stopped && !document.hidden) {
            setFrame('data:image/png;base64,' + result.png)
            setOffline(false)
          }
        } catch {
          if (!stopped) {
            setFrame(null)
            setOffline(true)
          }
        }
      } else if (!stopped) {
        setFrame(null)
      }
      if (!stopped) timer = setTimeout(refresh, 1000)
    }
    const hideFrame = () => { if (document.hidden) setFrame(null) }
    document.addEventListener('visibilitychange', hideFrame)
    void refresh()
    return () => {
      stopped = true
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', hideFrame)
    }
  }, [playing, visible, rest])

  return jsxs('section', {
    className: 'flex h-full flex-col gap-2 overflow-auto p-3',
    children: [
      jsx('button', {
        type: 'button',
        onClick: () => setPlaying(!playing),
        children: playing ? 'Остановить просмотр' : 'Смотреть экран',
      }),
      playing && frame
        ? jsx('img', { src: frame, alt: 'Текущий рабочий стол Windows', className: 'max-w-full object-contain' })
        : jsx('p', { role: 'status', children: playing ? (offline ? 'Экран недоступен' : 'Подключение…') : 'Просмотр выключен' }),
    ],
  })
}

export default {
  id: 'windows-pc',
  name: 'Windows PC',
  register(ctx) {
    const rest = (path, options) => ctx.rest(path, options)
    ctx.register({
      id: 'screen',
      area: 'panes',
      title: 'Windows PC',
      data: { placement: 'right', width: '460px' },
      render: () => jsx(WindowsScreen, { rest }),
    })
  },
}
