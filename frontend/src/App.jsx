import { useEffect, useMemo, useRef, useState } from 'react';

const LOG_LIMIT = 300;
const MESSAGE_LIMIT = 120;
const defaultHost = typeof window !== 'undefined' ? window.location.hostname : 'localhost';
const defaultScheme = typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss' : 'ws';
const DEFAULT_WS_URL = `${defaultScheme}://${defaultHost}:8012/ws/vibe`;

function nowIso() {
  return new Date().toISOString();
}

function speechRecognitionCtor() {
  if (typeof window === 'undefined') {
    return null;
  }
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function normalizeUserName(raw) {
  const text = String(raw ?? '').trim().toLowerCase();
  if (!text) {
    return 'vibe-user';
  }
  const collapsed = text.replace(/\s+/g, '-').replace(/[^a-z0-9._-]/g, '');
  return collapsed || 'vibe-user';
}

export default function App() {
  const [wsUrl, setWsUrl] = useState(DEFAULT_WS_URL);
  const [userName, setUserName] = useState('vibe-user');
  const [connected, setConnected] = useState(false);
  const [listening, setListening] = useState(false);
  const [sessionId, setSessionId] = useState('');
  const [statusText, setStatusText] = useState('disconnected');
  const [partialTranscript, setPartialTranscript] = useState('');
  const [messages, setMessages] = useState([]);
  const [eventLog, setEventLog] = useState([]);
  const [manualText, setManualText] = useState('');
  const [lastError, setLastError] = useState('');
  const [ttsEnabled, setTtsEnabled] = useState(true);
  const [ttsRate, setTtsRate] = useState('1');
  const [ttsPitch, setTtsPitch] = useState('1');
  const [voiceName, setVoiceName] = useState('');
  const [availableVoices, setAvailableVoices] = useState([]);

  const wsRef = useRef(null);
  const recognitionRef = useRef(null);
  const keepListeningRef = useRef(false);

  const recognitionSupported = useMemo(() => Boolean(speechRecognitionCtor()), []);

  const appendLog = (message) => {
    const row = `${nowIso()} ${String(message ?? '')}`;
    setEventLog((prev) => {
      const next = [...prev, row];
      if (next.length <= LOG_LIMIT) {
        return next;
      }
      return next.slice(next.length - LOG_LIMIT);
    });
  };

  const appendMessage = (role, text, source = '') => {
    const cleaned = String(text ?? '').trim();
    if (!cleaned) {
      return;
    }
    setMessages((prev) => {
      const row = { role, text: cleaned, ts: nowIso(), source };
      return [row, ...prev].slice(0, MESSAGE_LIMIT);
    });
  };

  const stopSpeechOutput = () => {
    if (typeof window === 'undefined' || !window.speechSynthesis) {
      return;
    }
    try {
      window.speechSynthesis.cancel();
    } catch (_error) {
      // Ignore best-effort cancellation failures.
    }
  };

  const speakAssistantText = (text) => {
    if (!ttsEnabled) {
      return;
    }
    if (typeof window === 'undefined' || !window.speechSynthesis || !window.SpeechSynthesisUtterance) {
      return;
    }
    const content = String(text ?? '').trim();
    if (!content) {
      return;
    }
    const utterance = new window.SpeechSynthesisUtterance(content);
    utterance.rate = Number(ttsRate) || 1;
    utterance.pitch = Number(ttsPitch) || 1;
    if (voiceName) {
      const match = window.speechSynthesis.getVoices().find((voice) => voice.name === voiceName);
      if (match) {
        utterance.voice = match;
      }
    }
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  };

  const sendEvent = (payload) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return false;
    }
    try {
      ws.send(JSON.stringify(payload));
      return true;
    } catch (_error) {
      return false;
    }
  };

  const sendUserText = (text, source = 'manual') => {
    const cleaned = String(text ?? '').trim();
    if (!cleaned) {
      return;
    }
    const user = normalizeUserName(userName);
    const sent = sendEvent({ event: 'user_text', user, text: cleaned });
    if (sent) {
      appendLog(`sent user_text (${source}): ${cleaned}`);
    } else {
      appendLog(`send failed (socket closed): ${cleaned}`);
      setLastError('WebSocket is not connected');
    }
  };

  const stopListeningNow = () => {
    keepListeningRef.current = false;
    const recognition = recognitionRef.current;
    if (!recognition) {
      setListening(false);
      return;
    }
    try {
      recognition.stop();
    } catch (_error) {
      // Ignore stop races.
    }
    recognitionRef.current = null;
    setListening(false);
    setPartialTranscript('');
    appendLog('voice listening stopped');
  };

  const startListeningNow = () => {
    if (!connected) {
      setLastError('Connect websocket first');
      return;
    }
    if (!recognitionSupported) {
      setLastError('Browser speech recognition is not supported in this browser');
      return;
    }
    if (recognitionRef.current) {
      return;
    }

    const Ctor = speechRecognitionCtor();
    if (!Ctor) {
      setLastError('Browser speech recognition is unavailable');
      return;
    }

    const recognition = new Ctor();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';

    recognition.onstart = () => {
      setListening(true);
      setLastError('');
      appendLog('voice listening started');
    };

    recognition.onresult = (event) => {
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const item = event.results[i];
        const transcript = String(item?.[0]?.transcript ?? '').trim();
        if (!transcript) {
          continue;
        }
        if (item.isFinal) {
          setPartialTranscript('');
          sendUserText(transcript, 'voice-final');
        } else {
          interim = `${interim} ${transcript}`.trim();
        }
      }
      setPartialTranscript(interim);
      if (interim) {
        sendEvent({
          event: 'partial_transcript',
          user: normalizeUserName(userName),
          text: interim,
        });
      }
    };

    recognition.onerror = (event) => {
      const errorCode = String(event?.error ?? 'unknown');
      appendLog(`voice recognition error: ${errorCode}`);
      setLastError(`Voice recognition error: ${errorCode}`);
    };

    recognition.onend = () => {
      recognitionRef.current = null;
      setListening(false);
      setPartialTranscript('');
      if (keepListeningRef.current && connected) {
        window.setTimeout(() => {
          if (!keepListeningRef.current || !connected) {
            return;
          }
          startListeningNow();
        }, 180);
      }
    };

    recognitionRef.current = recognition;
    keepListeningRef.current = true;
    try {
      recognition.start();
    } catch (error) {
      recognitionRef.current = null;
      keepListeningRef.current = false;
      setListening(false);
      setLastError(`Voice recognition start failed: ${String(error)}`);
    }
  };

  const handleServerEvent = (payload) => {
    const event = String(payload?.event ?? '').trim().toLowerCase();
    if (event && event !== 'partial_transcript') {
      appendLog(`server event: ${event}`);
    }

    if (event === 'ready') {
      setSessionId(String(payload?.session_id ?? ''));
      setStatusText('connected');
      setLastError('');
      return;
    }

    if (event === 'final_transcript') {
      const text = String(payload?.text ?? '').trim();
      const sender = normalizeUserName(payload?.user ?? userName);
      if (!text) {
        return;
      }
      appendMessage('user', `${sender}: ${text}`, 'server');
      return;
    }

    if (event === 'partial_transcript') {
      const text = String(payload?.text ?? '').trim();
      setPartialTranscript(text);
      return;
    }

    if (event === 'assistant_message' || event === 'assistant_response_done') {
      const text = String(payload?.text ?? '').trim();
      if (!text) {
        return;
      }
      appendMessage('assistant', text, 'server');
      speakAssistantText(text);
      return;
    }

    if (event === 'error') {
      const message = String(payload?.message ?? 'server error');
      setLastError(message);
      return;
    }
  };

  const connectWebSocket = () => {
    if (wsRef.current && wsRef.current.readyState <= WebSocket.OPEN) {
      return;
    }

    setLastError('');
    setStatusText('connecting');
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      setStatusText('connected');
      appendLog(`websocket connected: ${wsUrl}`);
      try {
        ws.send(JSON.stringify({ event: 'set_user', user: normalizeUserName(userName) }));
      } catch (_error) {
        // Best effort only.
      }
    };

    ws.onclose = () => {
      setConnected(false);
      setStatusText('disconnected');
      setSessionId('');
      stopListeningNow();
      appendLog('websocket closed');
    };

    ws.onerror = () => {
      setLastError('websocket error');
      appendLog('websocket error');
    };

    ws.onmessage = (message) => {
      try {
        const parsed = JSON.parse(String(message.data));
        handleServerEvent(parsed);
      } catch (error) {
        appendLog(`invalid server payload: ${String(error)}`);
      }
    };

  };

  const disconnectWebSocket = () => {
    stopListeningNow();
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    stopSpeechOutput();
    setConnected(false);
    setSessionId('');
    setStatusText('disconnected');
  };

  const sendManualMessage = () => {
    const text = manualText.trim();
    if (!text) {
      return;
    }
    setManualText('');
    sendUserText(text, 'manual');
  };

  useEffect(() => {
    if (typeof window === 'undefined' || !window.speechSynthesis) {
      return undefined;
    }

    const loadVoices = () => {
      const voices = window.speechSynthesis.getVoices() || [];
      setAvailableVoices(voices);
      if (!voiceName && voices.length > 0) {
        setVoiceName(voices[0].name);
      }
    };

    loadVoices();
    window.speechSynthesis.addEventListener('voiceschanged', loadVoices);
    return () => {
      window.speechSynthesis.removeEventListener('voiceschanged', loadVoices);
    };
  }, [voiceName]);

  useEffect(() => {
    return () => {
      keepListeningRef.current = false;
      const recognition = recognitionRef.current;
      if (recognition) {
        try {
          recognition.stop();
        } catch (_error) {
          // Ignore cleanup races.
        }
      }
      recognitionRef.current = null;
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      stopSpeechOutput();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="app-shell">
      <header className="hero">
        <h1>MeTTaClaw Vibe Voice</h1>
        <p>
          Voice frontend derived from the realtime client flow in
          {' '}
          <code>speech-audio-token-system</code>
          {' '}
          and connected to the
          {' '}
          <code>vibe-voice</code>
          {' '}
          channel.
        </p>
      </header>

      <section className="control-card">
        <div className="grid">
          <label>
            WebSocket URL
            <input value={wsUrl} onChange={(event) => setWsUrl(event.target.value)} disabled={connected} />
          </label>

          <label>
            User Name
            <input value={userName} onChange={(event) => setUserName(event.target.value)} disabled={connected} />
          </label>

          <label>
            TTS Voice
            <select value={voiceName} onChange={(event) => setVoiceName(event.target.value)}>
              {availableVoices.length === 0 ? <option value="">System Default</option> : null}
              {availableVoices.map((voice) => (
                <option key={voice.name} value={voice.name}>{voice.name}</option>
              ))}
            </select>
          </label>

          <label>
            TTS Rate
            <select value={ttsRate} onChange={(event) => setTtsRate(event.target.value)}>
              <option value="0.85">0.85x</option>
              <option value="1">1.0x</option>
              <option value="1.15">1.15x</option>
              <option value="1.3">1.3x</option>
            </select>
          </label>

          <label>
            TTS Pitch
            <select value={ttsPitch} onChange={(event) => setTtsPitch(event.target.value)}>
              <option value="0.9">0.9</option>
              <option value="1">1.0</option>
              <option value="1.1">1.1</option>
              <option value="1.2">1.2</option>
            </select>
          </label>

          <label className="toggle-row">
            <span>Enable Browser TTS</span>
            <input
              type="checkbox"
              checked={ttsEnabled}
              onChange={(event) => {
                setTtsEnabled(event.target.checked);
                if (!event.target.checked) {
                  stopSpeechOutput();
                }
              }}
            />
          </label>
        </div>

        <div className="button-row">
          <button onClick={connectWebSocket} disabled={connected}>Connect</button>
          <button onClick={disconnectWebSocket} disabled={!connected}>Disconnect</button>
          <button onClick={startListeningNow} disabled={!connected || listening || !recognitionSupported}>Start Voice</button>
          <button onClick={stopListeningNow} disabled={!listening}>Stop Voice</button>
          <button onClick={stopSpeechOutput} disabled={!ttsEnabled}>Stop TTS</button>
        </div>

        <div className="status-row">
          <span><strong>Status:</strong> {statusText}</span>
          <span><strong>Session:</strong> {sessionId || '-'}</span>
          <span><strong>Recognition:</strong> {recognitionSupported ? 'browser-supported' : 'unsupported'}</span>
        </div>

        {lastError ? <div className="error">Error: {lastError}</div> : null}
      </section>

      <section className="panel-grid">
        <article className="panel">
          <h2>Partial Transcript</h2>
          <p className="partial-text">{partialTranscript || '...listening'}</p>
        </article>

        <article className="panel tall">
          <h2>Manual Message</h2>
          <div className="manual-row">
            <textarea
              value={manualText}
              onChange={(event) => setManualText(event.target.value)}
              placeholder="Type a message and send it into the vibe-voice channel"
            />
            <button onClick={sendManualMessage} disabled={!connected || !manualText.trim()}>Send</button>
          </div>
        </article>

        <article className="panel tall">
          <h2>Conversation</h2>
          <ul className="list">
            {messages.map((row, index) => (
              <li key={`${row.ts}-${index}`} className={row.role === 'assistant' ? 'assistant' : 'user'}>
                <span className="ts">{row.ts}</span>
                <span>{row.text}</span>
              </li>
            ))}
            {messages.length === 0 ? <li className="muted">No messages yet.</li> : null}
          </ul>
        </article>
      </section>

      <section className="panel log-panel">
        <h2>Event Log</h2>
        <pre>{eventLog.join('\n') || 'No events yet.'}</pre>
      </section>
    </div>
  );
}
