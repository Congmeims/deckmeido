import deckyPlugin from '@decky/rollup';
import { readFileSync } from 'fs';

// 插件版本号从 package.json 注入，避免界面里再手抄一份（抄了就会和主程序对不上）
const pkg = JSON.parse(readFileSync('./package.json', 'utf-8'));

export default deckyPlugin({
  plugins: [
    {
      name: 'plugin-version',
      transform(code, id) {
        if (!id.includes('/src/') && !id.includes('\\src\\')) return null;
        return { code: code.replace(/__PLUGIN_VERSION__/g, JSON.stringify(pkg.version)), map: null };
      },
    },
  ],
});
