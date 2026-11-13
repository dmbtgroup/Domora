// Make file uploads reliable on phones.
//
// Chrome on Android fails with ERR_UPLOAD_FILE_CHANGED when the picked file changes or is still
// downloading (a fresh camera photo, Google Photos, Drive) by the time the form is sent. So as soon
// as a file is picked we keep an in-memory copy and send that instead. Large photos are also scaled
// down so they fit the 4 MB upload limit.
(function () {
  var LIMIT = 4 * 1024 * 1024, BIG_PHOTO = 1.5 * 1024 * 1024, MAX_SIDE = 2200;
  var pending = new WeakMap();  // form -> promise of files still being copied

  function t(en, fr) { return (document.documentElement.lang || 'en').indexOf('fr') === 0 ? fr : en; }

  function shrink(file) {
    if (!/^image\/(jpeg|png|webp)$/.test(file.type) || file.size <= BIG_PHOTO || !window.createImageBitmap) {
      return Promise.resolve(null);
    }
    return createImageBitmap(file).then(function (img) {
      var scale = Math.min(1, MAX_SIDE / Math.max(img.width, img.height));
      var canvas = document.createElement('canvas');
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
      return new Promise(function (resolve) { canvas.toBlob(resolve, 'image/jpeg', 0.85); });
    }).then(function (blob) {
      if (!blob || blob.size >= file.size) return null;
      var name = file.name.replace(/\.(png|webp|jpe?g)$/i, '') + '.jpg';
      return new File([blob], name, { type: 'image/jpeg', lastModified: Date.now() });
    }).catch(function () { return null; });
  }

  function copy(file) {
    return shrink(file).then(function (small) {
      if (small) return small;
      return file.arrayBuffer().then(function (buf) {
        return new File([buf], file.name, { type: file.type, lastModified: file.lastModified });
      });
    });
  }

  document.addEventListener('change', function (e) {
    var input = e.target;
    if (!input.matches || !input.matches('input[type=file]') || !input.files || !input.files.length) return;
    if (typeof DataTransfer === 'undefined') return;
    var form = input.form;
    var job = Promise.all(Array.prototype.map.call(input.files, copy)).then(function (files) {
      var dt = new DataTransfer();
      files.forEach(function (f) { dt.items.add(f); });
      input.files = dt.files;
      var tooBig = files.filter(function (f) { return f.size > LIMIT; });
      if (tooBig.length) {
        alert(t('This file is larger than 4 MB, so it can’t be uploaded: ', 'Ce fichier dépasse 4 Mo et ne peut pas être téléversé : ') + tooBig[0].name);
        input.value = '';
      }
    }).catch(function () {
      alert(t('The file couldn’t be read. If it’s still downloading to your phone (Google Photos, Drive), wait a moment and pick it again.',
              'Le fichier n’a pas pu être lu. S’il est encore en téléchargement sur votre téléphone (Google Photos, Drive), attendez un instant et choisissez-le de nouveau.'));
      input.value = '';
    });
    if (form) {
      pending.set(form, job);
      job.then(function () { if (pending.get(form) === job) pending.delete(form); });
    }
  }, true);

  // A form sent while a file is still being copied waits for it, then is sent.
  document.addEventListener('submit', function (e) {
    var form = e.target, job = pending.get(form);
    if (!job) return;
    e.preventDefault();
    var btn = e.submitter;
    if (btn) btn.disabled = true;
    job.then(function () {
      pending.delete(form);
      if (btn) btn.disabled = false;
      if (form.requestSubmit) form.requestSubmit(btn || undefined); else form.submit();
    });
  }, true);
})();
