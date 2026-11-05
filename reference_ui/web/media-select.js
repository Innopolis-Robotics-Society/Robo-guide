/* Media selection used by the real renderer and Node tests. */
(function (root) {
  'use strict';
  const resolveChunkId = (ids, index) => index >= 0 && index < ids.length ? ids[index] : null;
  const selectMedia = (items, chunkId) => {
    const matching = chunkId === null ? [] : items.filter(item => item.chunk_id === chunkId);
    return matching.length ? matching : items.filter(item => item.chunk_id === '');
  };
  const api = {resolveChunkId, selectMedia};
  if (typeof module !== 'undefined') module.exports = api;
  else root.MediaSelect = api;
})(typeof window === 'undefined' ? globalThis : window);
