import React, { useEffect, useRef, useState } from 'react';

// Animated .tgs stickers: gzipped Lottie JSON. lottie-web is loaded lazily (it is big).
const cache = new Map();

async function loadAnimation(url) {
  if (!cache.has(url)) {
    cache.set(
      url,
      (async () => {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const stream = res.body.pipeThrough(new DecompressionStream('gzip'));
        return JSON.parse(await new Response(stream).text());
      })(),
    );
  }
  return cache.get(url);
}

export default function Lottie({ url, fallback }) {
  const ref = useRef(null);
  const [failed, setFailed] = useState(typeof DecompressionStream === 'undefined');
  useEffect(() => {
    if (failed) return undefined;
    let anim;
    let alive = true;
    Promise.all([loadAnimation(url), import('lottie-web/build/player/lottie_light.js')])
      .then(([data, mod]) => {
        if (!alive || !ref.current) return;
        anim = mod.default.loadAnimation({
          container: ref.current,
          renderer: 'svg',
          loop: true,
          autoplay: true,
          animationData: structuredClone(data), // lottie mutates its input
        });
      })
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
      anim?.destroy();
    };
  }, [url, failed]);
  if (failed) return fallback;
  return <div className="sticker" ref={ref} />;
}
