// sk_cast.js -- TV-cast helper for the SKChat web app (Cast-to-TV sprint 2).
//
// The Flutter app renders a container <div> (an HtmlElementView platform view)
// and hands its DOM id to this helper. We create + own the <video> element that
// plays the room's HLS stream, and expose the three cast paths:
//
//   1. AirPlay  -- Safari plays HLS natively, so the <video> gets the native
//                  AirPlay route button (x-webkit-airplay="allow"). We also
//                  expose webkitShowPlaybackTargetPicker() for a custom button.
//   2. Chromecast -- the Google Cast SDK (CAF). requestCast() opens the device
//                  picker and loads the hls_url on the default media receiver
//                  (which plays HLS).
//   3. hls.js   -- Chrome / non-Safari cannot play HLS in <video> natively, so
//                  we lazy-load hls.js from a CDN and attach it to the element.
//
// The guaranteed fallback (copy / open the hls_url) lives in Dart. This file is
// vendored (loaded from web/index.html) so the tricky browser + SDK glue stays
// in JS and the Dart side only makes small dynamic calls.
//
// No em/en dashes anywhere in this file (house rule).

(function () {
  "use strict";

  var HLS_JS_SRC =
    "https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js";
  var CAST_SDK_SRC =
    "https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1";
  // Default Media Receiver app id (plays HLS / MP4 out of the box).
  var DEFAULT_RECEIVER = "CC1AD845";

  // Per-mount state, keyed by the container DOM id.
  var mounts = {};
  var castReady = false;

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var existing = document.querySelector('script[data-skcast="' + src + '"]');
      if (existing) {
        if (existing.getAttribute("data-loaded") === "1") {
          resolve();
        } else {
          existing.addEventListener("load", function () {
            resolve();
          });
          existing.addEventListener("error", function () {
            reject(new Error("failed to load " + src));
          });
        }
        return;
      }
      var s = document.createElement("script");
      s.src = src;
      s.async = true;
      s.setAttribute("data-skcast", src);
      s.onload = function () {
        s.setAttribute("data-loaded", "1");
        resolve();
      };
      s.onerror = function () {
        reject(new Error("failed to load " + src));
      };
      document.head.appendChild(s);
    });
  }

  // Initialise the Cast framework once the SDK reports availability.
  window.__onGCastApiAvailable = function (isAvailable) {
    if (!isAvailable) return;
    try {
      var ctx = cast.framework.CastContext.getInstance();
      ctx.setOptions({
        receiverApplicationId: DEFAULT_RECEIVER,
        autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED,
      });
      castReady = true;
    } catch (e) {
      castReady = false;
    }
  };

  // Kick off the Cast SDK load early (harmless if no cast devices exist).
  loadScript(CAST_SDK_SRC).catch(function () {
    /* cast unavailable; AirPlay + hls.js + open-URL still work */
  });

  function isSafariNativeHls(video) {
    try {
      return !!video.canPlayType("application/vnd.apple.mpegurl");
    } catch (e) {
      return false;
    }
  }

  function makeVideo() {
    var video = document.createElement("video");
    video.setAttribute("playsinline", "");
    video.setAttribute("webkit-playsinline", "");
    // Let iOS / Safari route this element over AirPlay and expose the button.
    video.setAttribute("x-webkit-airplay", "allow");
    video.controls = true;
    video.autoplay = true;
    video.muted = false;
    video.style.position = "absolute";
    video.style.top = "0";
    video.style.left = "0";
    video.style.width = "100%";
    video.style.height = "100%";
    video.style.backgroundColor = "#000";
    return video;
  }

  function attachHls(state) {
    var video = state.video;
    var url = state.hlsUrl;
    if (isSafariNativeHls(video)) {
      state.nativeHls = true;
      video.src = url;
      video.load();
      var p = video.play();
      if (p && p.catch) p.catch(function () {});
      return;
    }
    // Non-Safari: use hls.js.
    loadScript(HLS_JS_SRC)
      .then(function () {
        if (state.disposed) return;
        var HlsCtor = window.Hls;
        if (HlsCtor && HlsCtor.isSupported()) {
          var hls = new HlsCtor({ lowLatencyMode: true });
          state.hls = hls;
          hls.loadSource(url);
          hls.attachMedia(video);
          hls.on(HlsCtor.Events.MANIFEST_PARSED, function () {
            var p = video.play();
            if (p && p.catch) p.catch(function () {});
          });
        } else {
          // Last resort: hand the URL straight to the element.
          video.src = url;
          video.load();
        }
      })
      .catch(function () {
        video.src = url;
        video.load();
      });
  }

  var skCast = {
    // Mount a video that plays hlsUrl inside the container with DOM id `id`.
    mount: function (id, hlsUrl) {
      var container = document.getElementById(id);
      if (!container) return false;
      // Tear down any previous mount on this container.
      skCast.unmount(id);
      var video = makeVideo();
      container.appendChild(video);
      var state = {
        container: container,
        video: video,
        hlsUrl: hlsUrl,
        hls: null,
        nativeHls: false,
        disposed: false,
      };
      mounts[id] = state;
      attachHls(state);
      return true;
    },

    // True when the mounted element can offer a native AirPlay picker (Safari).
    airplayAvailable: function (id) {
      var state = mounts[id];
      if (!state) return false;
      return typeof state.video.webkitShowPlaybackTargetPicker === "function";
    },

    // Show Safari's AirPlay device picker for the mounted element.
    showAirplay: function (id) {
      var state = mounts[id];
      if (!state) return false;
      if (typeof state.video.webkitShowPlaybackTargetPicker === "function") {
        try {
          state.video.webkitShowPlaybackTargetPicker();
          return true;
        } catch (e) {
          return false;
        }
      }
      return false;
    },

    // True once the Cast SDK is initialised and a session can be requested.
    castAvailable: function () {
      return (
        castReady &&
        typeof window.cast !== "undefined" &&
        !!window.cast.framework
      );
    },

    // Open the Chromecast device picker and load the mount's hls_url on the
    // receiver. Returns a Promise that resolves true on a load request sent.
    requestCast: function (id) {
      var state = mounts[id];
      if (!state) return Promise.resolve(false);
      if (!skCast.castAvailable()) return Promise.resolve(false);
      var ctx = cast.framework.CastContext.getInstance();

      function loadOnSession() {
        var session = ctx.getCurrentSession();
        if (!session) return false;
        var mediaInfo = new chrome.cast.media.MediaInfo(
          state.hlsUrl,
          "application/x-mpegURL"
        );
        mediaInfo.streamType = chrome.cast.media.StreamType.LIVE;
        var request = new chrome.cast.media.LoadRequest(mediaInfo);
        session.loadMedia(request);
        return true;
      }

      // Already connected to a receiver: just (re)load our media.
      if (
        ctx.getCastState &&
        ctx.getCastState() === cast.framework.CastState.CONNECTED
      ) {
        return Promise.resolve(loadOnSession());
      }
      // Otherwise open the device picker, then load once connected.
      return ctx.requestSession().then(
        function () {
          return loadOnSession();
        },
        function () {
          return false;
        }
      );
    },

    // End any Chromecast session (best-effort).
    endCast: function () {
      try {
        if (skCast.castAvailable()) {
          cast.framework.CastContext.getInstance().endCurrentSession(true);
        }
      } catch (e) {
        /* ignore */
      }
    },

    // Stop playback and remove the video for this container.
    unmount: function (id) {
      var state = mounts[id];
      if (!state) return;
      state.disposed = true;
      try {
        if (state.hls) {
          state.hls.destroy();
          state.hls = null;
        }
      } catch (e) {
        /* ignore */
      }
      try {
        state.video.pause();
        state.video.removeAttribute("src");
        state.video.load();
        if (state.video.parentNode) {
          state.video.parentNode.removeChild(state.video);
        }
      } catch (e) {
        /* ignore */
      }
      delete mounts[id];
    },
  };

  window.skCast = skCast;
})();
