/* Tailwind 构建配置（仅构建期使用，运行时不加载）。见同目录 BUILD.md。 */
module.exports = {
  content: ['./index.html', './app.js'],
  darkMode: 'selector',
  theme: { extend: {} },
  corePlugins: { preflight: true },
};
