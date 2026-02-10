const { chromium } = require('playwright');

const TARGET_URL = 'http://127.0.0.1:4096';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded', timeout: 10000 });
  await page.waitForTimeout(2000);

  // Screenshot 1: Default layout
  await page.screenshot({ path: '/tmp/layout-default.png', fullPage: false });
  console.log('Screenshot 1: /tmp/layout-default.png');

  // Check grid
  const gridStyle = await page.$eval('.app-layout', el => window.getComputedStyle(el).gridTemplateColumns);
  console.log('Grid columns:', gridStyle);

  // Bounding boxes
  for (const sel of ['.activity-bar', '.sidebar', '.main-content', '.observability-panel']) {
    const el = await page.$(sel);
    const box = el ? await el.boundingBox() : null;
    console.log(sel + ':', box ? `x=${Math.round(box.x)} w=${Math.round(box.width)} h=${Math.round(box.height)}` : 'NOT FOUND');
  }

  const handles = await page.$$('.resize-handle');
  console.log('Resize handles:', handles.length);
  for (let i = 0; i < handles.length; i++) {
    const box = await handles[i].boundingBox();
    const style = await handles[i].evaluate(el => el.getAttribute('style'));
    console.log(`  Handle ${i}: style="${style}" box=${box ? `x=${Math.round(box.x)} w=${box.width} h=${Math.round(box.height)}` : 'null'}`);
  }

  // Hover left handle
  if (handles.length > 0) {
    const box = await handles[0].boundingBox();
    if (box) {
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForTimeout(300);
      await page.screenshot({ path: '/tmp/layout-hover.png', fullPage: false });
      console.log('Screenshot 2: /tmp/layout-hover.png (handle hover)');
    }
  }

  await browser.close();
  console.log('Done');
})();
