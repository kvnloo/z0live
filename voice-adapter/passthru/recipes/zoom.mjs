// Zoom: /j/ID links open the web client at /wc/join/ID.
export default {
  name: 'zoom',
  match: /zoom\.us\/(j|wc)\//,
  rewrite: u => u.replace(/zoom\.us\/j\/(\d+)/, 'zoom.us/wc/join/$1'),
  async join(page, cfg) {
    const name = page.locator('#input-for-name, input[aria-label*="name" i]');
    if (await name.first().waitFor({ state: "visible", timeout: 20000 }).then(() => true, () => false)) await name.first().fill(cfg.name);
    await page.getByRole('button', { name: /^Join$/ }).first().click({ timeout: 20000 });
  },
};
