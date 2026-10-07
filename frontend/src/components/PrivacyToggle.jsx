import React from 'react';
import { setHideMedia, useHideMedia } from '../lib/privacy.js';

/** 👁 / 🙈 button: hide photos, videos, stickers and profile pictures (e.g. in public). */
export default function PrivacyToggle() {
  const hidden = useHideMedia();
  return (
    <button
      type="button"
      className={`icon-btn privacy-btn${hidden ? ' on' : ''}`}
      onClick={() => setHideMedia(!hidden)}
      aria-pressed={hidden}
      aria-label={hidden ? 'Show media' : 'Hide media'}
      title={hidden ? 'Media hidden — click to show' : 'Hide media (for use in public)'}
    >
      {hidden ? '🙈' : '👁'}
    </button>
  );
}
