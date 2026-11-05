import { useEffect, useRef, useState } from 'react'
import './App.css'
import rangeRoverAsset from './assets/RANGEROVER TOP VIEW.png'

const NODE_ORDER = ['FL', 'FR', 'RL', 'RR']

const DEFAULT_WHEELS = {
  FL: { pressure: '2.30', temp: '33', status: 'NORMAL', lastSeen: Date.now() },
  FR: { pressure: '2.27', temp: '31', status: 'NORMAL', lastSeen: Date.now() },
  RL: { pressure: '2.27', temp: '32', status: 'NORMAL', lastSeen: Date.now() },
  RR: { pressure: '2.61', temp: '26', status: 'NORMAL', lastSeen: Date.now() },
}

const positionMap = {
  FL: { left: '18%', top: '18%' },
  FR: { left: '18%', bottom: '18%' },
  RL: { right: '18%', top: '18%' },
  RR: { right: '18%', bottom: '18%' },
}

function normalizeStatus(pressure, temp, statusText) {
  const s = String(statusText || '').trim().toUpperCase()
  if (s === 'LOW' || pressure < 1.5) return 'LOW'
  if (s === 'HIGH' || pressure > 4.5) return 'HIGH'
  if (s === 'OVERHEAT' || temp > 45) return 'OVERHEAT'
  if (s === 'NO SIGNAL') return 'NO SIGNAL'
  return 'NORMAL'
}

function parseLine(line) {
  if (!line || !line.includes(',')) return null

  const parts = line.split(',').map((value) => value.trim())
  if (parts.length !== 4) return null

  const [node, pressureRaw, tempRaw, statusRaw] = parts
  if (!NODE_ORDER.includes(node)) return null

  const pressure = Number.parseFloat(pressureRaw)
  const temp = Number.parseFloat(tempRaw)
  if (Number.isNaN(pressure) || Number.isNaN(temp)) return null

  return {
    node,
    pressure,
    temp,
    status: normalizeStatus(pressure, temp, statusRaw),
  }
}

function App() {
  const [wheelValues, setWheelValues] = useState(DEFAULT_WHEELS)
  const [isConnected, setIsConnected] = useState(false)
  const [connectionText, setConnectionText] = useState('Not connected')
  const serialRef = useRef(null)
  const readerRef = useRef(null)

  const updateWheelState = (node, data) => {
    setWheelValues((prev) => ({
      ...prev,
      [node]: {
        pressure: data.pressure.toFixed(2),
        temp: data.temp.toFixed(1),
        status: normalizeStatus(data.pressure, data.temp, data.status),
        lastSeen: Date.now(),
      },
    }))
  }

  useEffect(() => {
    const timer = setInterval(() => {
      setWheelValues((prev) => {
        const next = { ...prev }
        let changed = false

        for (const node of NODE_ORDER) {
          const wheel = next[node]
          if (Date.now() - wheel.lastSeen > 3000) {
            if (wheel.status !== 'NO SIGNAL') {
              next[node] = { ...wheel, status: 'NO SIGNAL', lastSeen: Date.now() }
              changed = true
            }
          }
        }

        return changed ? next : prev
      })
    }, 1000)

    return () => clearInterval(timer)
  }, [])

  const connectDevice = async () => {
    if (!navigator.serial) {
      setConnectionText('Web Serial not supported in this browser')
      return
    }

    try {
      const port = await navigator.serial.requestPort()
      await port.open({ baudRate: 115200 })
      serialRef.current = port
      const reader = port.readable.getReader()
      readerRef.current = reader
      setIsConnected(true)
      setConnectionText('Connected')

      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { value, done } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split(/\r?\n/)
        buffer = lines.pop() || ''

        for (const line of lines) {
          const parsed = parseLine(line)
          if (!parsed) continue
          updateWheelState(parsed.node, parsed)
        }
      }
    } catch (error) {
      console.error('Serial connection failed:', error)
      setConnectionText('Connection failed')
      setIsConnected(false)
    }
  }

  const disconnectDevice = async () => {
    try {
      if (readerRef.current) {
        await readerRef.current.cancel()
      }
      if (serialRef.current) {
        await serialRef.current.close()
      }
    } catch (error) {
      console.error('Disconnect failed:', error)
    } finally {
      readerRef.current = null
      serialRef.current = null
      setIsConnected(false)
      setConnectionText('Not connected')
    }
  }

  return (
    <div className="tpms-page">
      <div className="tpms-dashboard">
        <div className="top-bar">
          <div className="status-indicator">
            <span className={`dot ${isConnected ? 'online' : 'offline'}`} />
            {connectionText}
          </div>

          <button className="serial-button" onClick={isConnected ? disconnectDevice : connectDevice}>
            {isConnected ? 'Disconnect' : 'Connect device'}
          </button>
        </div>

        <div className="vehicle-scene">
          <img src={rangeRoverAsset} alt="Range Rover top view" className="range-rover-photo" />

          {NODE_ORDER.map((node) => {
            const wheel = wheelValues[node]
            const pos = positionMap[node]

            return (
              <div
                key={node}
                className={`wheel-card status-${wheel.status.toLowerCase().replace(/\s+/g, '-')}`}
                style={{
                  left: pos.left,
                  right: pos.right,
                  top: pos.top,
                  bottom: pos.bottom,
                }}
              >
                <div className="wheel-pressure-row">
                  <span className="wheel-value">{wheel.pressure}bar</span>
                  <span className="wheel-temp">{wheel.temp}°C</span>
                </div>
                <div className="wheel-state">{wheel.status}</div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

export default App
