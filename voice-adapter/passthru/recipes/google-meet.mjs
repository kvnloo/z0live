// Google Meet: guest join screen (Kevin admits "z0" from inside the meeting).
export default {
  name: 'google-meet',
  match: /meet\.google\.com/,
  async join(page, cfg) {
    const name = page.locator('input[aria-label="Your name"], input[placeholder="Your name"]');
    if (await name.first().waitFor({ state: "visible", timeout: 15000 }).then(() => true, () => false)) await name.first().fill(cfg.name);
    await page.getByRole('button', { name: /^(Ask to join|Join now|Join)$/ }).first().click({ timeout: 20000 });
  },
  async share(page) { await page.getByRole('button', { name: /Share screen|Present now/ }).first().click({ timeout: 8000 }); },
  async unshare(page) { await page.getByRole('button', { name: /Stop (sharing|presenting)/ }).first().click({ timeout: 8000 }).catch(() => {}); },
  async leave(page) { await page.getByRole('button', { name: /Leave call/ }).first().click({ timeout: 5000 }).catch(() => {}); },
};
