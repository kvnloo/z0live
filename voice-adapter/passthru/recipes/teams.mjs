// Microsoft Teams: stay in the browser.
export default {
  name: 'teams',
  match: /teams\.(microsoft|live)\.com/,
  async join(page, cfg) {
    await page.getByRole('button', { name: /Continue on this browser/ }).first().click({ timeout: 15000 }).catch(() => {});
    const name = page.locator('input[placeholder*="name" i]');
    if (await name.first().waitFor({ state: "visible", timeout: 20000 }).then(() => true, () => false)) await name.first().fill(cfg.name);
    await page.getByRole('button', { name: /^Join now$/ }).first().click({ timeout: 20000 });
  },
};
