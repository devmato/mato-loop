import { configure } from 'quasar/wrappers'

const backendPort = Number(process.env.MATO_LOOP_BIND_BACKEND_PORT || 6020)
const frontendPort = Number(process.env.MATO_LOOP_BIND_FRONTEND_PORT || 10020)
const apiProxyTarget = process.env.QUASAR_API_PROXY_TARGET || `http://127.0.0.1:${backendPort}`

export default configure(() => ({
  boot: ['axios', 'appearance', 'confirm'],
  css: ['app.scss'],
  extras: ['roboto-font', 'material-icons'],
  store: true,
  build: {
    target: {
      browser: ['es2022', 'firefox115', 'chrome115', 'safari14'],
      node: 'node22'
    },
    vueRouterMode: 'history'
  },
  devServer: {
    open: false,
    port: frontendPort,
    proxy: {
      '/api': {
        target: apiProxyTarget,
        ws: true
      },
      '/manifest.webmanifest': { target: apiProxyTarget },
      '/docs': {
        target: apiProxyTarget,
        changeOrigin: true
      }
    }
  },
  framework: {
    config: {
      dark: 'auto',
      ripple: false,
      notify: {
        position: 'top-right'
      }
    },
    lang: 'de-DE',
    plugins: ['Dialog', 'Loading', 'Notify']
  },
  pwa: {
    workboxMode: 'InjectManifest',
    swFilename: 'sw.js',
    manifestFilename: 'manifest.webmanifest',
    injectPwaMetaTags: false,
    extendInjectManifestOptions(options) {
      options.globPatterns = ['**/*.{css,html,ico,js,png,svg,webmanifest,woff,woff2}']
      options.globIgnores = [...(options.globIgnores || []), 'manifest.webmanifest']
    }
  },
  animations: ['fadeIn', 'fadeOut']
}))
