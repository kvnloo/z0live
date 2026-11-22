// Element Call (call.element.io): open source, E2EE by default, guests need no account.
// The room password rides in the URL fragment, so it never reaches Element's web server.
// Selectors are Element Call's own data-testids (its e2e suite uses them), so they survive restyles.
const tid = (page, id) => page.locator(`[data-testid="${id}"]`);

export default {
  name: 'element-call',
  match: /call\.element\.io\//,
  home: 'https://call.element.io/',
  async create(page, cfg) {
    await page.goto(this.home, { waitUntil: 'domcontentloaded' });
    await page.getByLabel('Name of call').fill(cfg.title || 'z0-' + Math.random().toString(36).slice(2, 8));
    await page.getByLabel('Display name').fill(cfg.name);
    await page.getByRole('button', { name: 'Go' }).click();
    await page.waitForURL(/\/room\/#\/.+password=/, { timeout: 30000 });
    return page.url();
  },
  async join(page, cfg) {
    const guest = tid(page, 'joincall_joincall');                 // "Join as guest" form for link visitors
    if (await guest.waitFor({ state: "visible", timeout: 8000 }).then(() => true, () => false)) {
      await page.getByLabel('Display name').fill(cfg.name);
      await guest.click();
    }
    await tid(page, 'lobby_joinCall').click({ timeout: 30000 });
    await tid(page, 'incall_leave').waitFor({ timeout: 30000 });
  },
  async share(page) {
    const sw = tid(page, 'incall_screenshare');
    if ((await sw.getAttribute('aria-checked', { timeout: 8000 })) !== 'true') await sw.click();
  },
  async unshare(page) {
    const sw = tid(page, 'incall_screenshare');
    if ((await sw.getAttribute('aria-checked').catch(() => 'false')) === 'true') await sw.click();
  },
  async leave(page) { await tid(page, 'incall_leave').click({ timeout: 5000 }).catch(() => {}); },
};
