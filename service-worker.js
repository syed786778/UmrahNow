/*
 * UmrahNow service worker
 * ------------------------------------------------------------------
 * Scope: supports the Ihram Mode reminder feature and basic PWA
 * installability. This file does NOT implement server-triggered Web
 * Push — there is no backend behind this site. Notifications shown
 * here are always requested by an open UmrahNow tab, via
 * `registration.showNotification()`, so the browser process needs to
 * be running (the tab can be backgrounded/minimised, but not fully
 * closed) for a reminder to appear as a system notification.
 *
 * If UmrahNow adds a real backend later, true Web Push (notifications
 * that arrive even with every tab and the browser closed) would be
 * added here via a `push` event listener reading a payload sent by
 * that server — see the project notes for what that requires.
 * ------------------------------------------------------------------
 */

const CACHE_NAME = 'umrahnow-shell-v1';

// Activate the new worker immediately rather than waiting for old
// tabs to close — this is a small site, not a large cached app shell,
// so there's little risk in switching over right away.
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

/*
 * Notification click handling for Ihram Mode reminders.
 * Two actions are offered where the platform supports notification
 * actions ("Remind me later" / "Open UmrahNow"); browsers that don't
 * support actions just get the default click behaviour (open/focus).
 */
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  if (event.action === 'remind-later') {
    // We can only actually reschedule from a page that's open — tell
    // any open UmrahNow tabs to snooze the reminder themselves.
    event.waitUntil(
      self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
        clients.forEach((client) => client.postMessage({ type: 'ihram-remind-later' }));
      })
    );
    return;
  }

  // Default behaviour ("Open UmrahNow" or a plain tap): focus an
  // existing tab if one is open, otherwise open a new one at the
  // Journey section where Ihram Mode lives.
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ('focus' in client) {
          client.postMessage({ type: 'ihram-open-journey' });
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow('./#journey');
      }
      return null;
    })
  );
});
