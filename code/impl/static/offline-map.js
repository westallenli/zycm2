(function () {
  'use strict';
  // Local pixels only. A missing tile is cropped from its geographic ancestor;
  // the server still returns 404 for missing native detail and revoked sources.
  window.createOfflineMapLayer = function (L, url, options) {
    const cache = new Map();
    function imageAt(z, x, y) {
      const target = L.Util.template(url, { z, x, y });
      if (!cache.has(target)) {
        const promise = new Promise((resolve, reject) => {
          const image = new Image();
          image.onload = () => resolve(image);
          image.onerror = () => reject(Error('local tile unavailable'));
          image.src = target;
        });
        cache.set(target, promise);
        if (cache.size > 256) cache.delete(cache.keys().next().value);
      }
      return cache.get(target);
    }
    const Layer = L.TileLayer.extend({
      createTile: function (coords, done) {
        const tile = document.createElement('img');
        tile.alt = ''; tile.setAttribute('role', 'presentation');
        const min = this.options.minZoom || 0;
        (async () => {
          for (let z = coords.z; z >= min; z--) {
            const scale = Math.pow(2, coords.z - z);
            const x = Math.floor(coords.x / scale), y = Math.floor(coords.y / scale);
            let image;
            try { image = await imageAt(z, x, y); } catch (_) { continue; }
            let src = image.src;
            if (z < coords.z) {
              const canvas = document.createElement('canvas');
              canvas.width = canvas.height = this.options.tileSize || 256;
              const width = image.naturalWidth / scale, height = image.naturalHeight / scale;
              const sx = (coords.x - x * scale) * width, sy = (coords.y - y * scale) * height;
              canvas.getContext('2d').drawImage(image, sx, sy, width, height, 0, 0, canvas.width, canvas.height);
              src = canvas.toDataURL('image/png');
              tile.title = '离线原图第' + z + '级放大；当前没有第' + coords.z + '级细节';
            }
            tile.dataset.nativeZoom = z; tile.dataset.requestedZoom = coords.z;
            tile.onload = () => { done(null, tile); };
            tile.onerror = () => done(Error('local image decode failed'), tile);
            tile.src = src;
            return;
          }
          done(Error('no local ancestor tile'), tile);
        })().catch(error => done(error, tile));
        return tile;
      }
    });
    return new Layer(url, options);
  };
})();
