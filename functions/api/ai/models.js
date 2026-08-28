import { json, activeKey, readSettings, DEFAULT_MODEL } from '../../_lib.js';

const FALLBACK = [
  { id: 'gemini-flash-latest', label: 'Gemini Flash (항상 최신)', family: '항상 최신 (별칭)' },
  { id: 'gemini-pro-latest', label: 'Gemini Pro (항상 최신)', family: '항상 최신 (별칭)' },
  { id: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash', family: 'Gemini 2.5' },
  { id: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro', family: 'Gemini 2.5' },
];
const SKIP = ['embedding', 'aqa', 'imagen', 'veo', '-tts', 'image-generation', 'native-audio',
  '-live', 'computer-use', '-image', 'nano-banana', 'lyria', 'transcribe', 'robotics', 'antigravity'];

function family(id) {
  const l = id.toLowerCase();
  if (l.endsWith('-latest')) return '항상 최신 (별칭)';
  const m = l.match(/^gemini-(\d+)(?:[.\-](\d+))?/);
  if (m) return 'Gemini ' + (m[2] && m[2] !== '0' ? `${m[1]}.${m[2]}` : m[1]);
  if (l.startsWith('gemma')) return 'Gemma';
  return '기타';
}
function rank(id) {
  const l = id.toLowerCase();
  const tier = l.includes('-pro') ? 0 : l.includes('flash-lite') ? 2 : l.includes('flash') ? 1 : 3;
  const stage = /preview|-exp|experimental/.test(l) ? 1 : 0;
  if (l.endsWith('-latest')) return [0, 0, 0, tier, 0, l];
  const m = l.match(/^gemini-(\d+)(?:[.\-](\d+))?/);
  if (m) return [1, -Number(m[1]), -Number(m[2] || 0), tier, stage, l];
  return [2, 0, 0, tier, stage, l];
}

export async function onRequestGet({ env }) {
  const s = await readSettings(env);
  const { key } = await activeKey(env);
  const current = s.model || DEFAULT_MODEL;
  if (!key) return json({ models: FALLBACK, live: false, current });
  try {
    const res = await fetch('https://generativelanguage.googleapis.com/v1beta/models?pageSize=200',
      { headers: { 'x-goog-api-key': key } });
    const data = await res.json();
    const models = (data.models || [])
      .filter(m => (m.supportedGenerationMethods || []).includes('generateContent'))
      .map(m => ({ id: (m.name || '').split('/').pop(), label: m.displayName || '',
                   note: (m.description || '').slice(0, 110), input_limit: m.inputTokenLimit || 0 }))
      .filter(m => m.id && !SKIP.some(p => m.id.toLowerCase().includes(p)))
      .map(m => ({ ...m, label: m.label || m.id, family: family(m.id) }));
    models.sort((a, b) => {
      const x = rank(a.id), y = rank(b.id);
      for (let i = 0; i < x.length; i++) { if (x[i] < y[i]) return -1; if (x[i] > y[i]) return 1; }
      return 0;
    });
    return json({ models: models.length ? models : FALLBACK, live: models.length > 0, current });
  } catch (e) {
    return json({ models: FALLBACK, live: false, current, error: String(e.message || e) });
  }
}
