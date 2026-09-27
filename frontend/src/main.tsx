// frontend/src/main.tsx
import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { loadBranding } from './lib/branding'
import './index.css'

// Branding is fetched before the first render, so the sign-in page names the
// product correctly the first time rather than flashing the engine's name and
// correcting itself. loadBranding never rejects and gives up after 2s, so a
// deployment whose API is down still renders, with the built-in defaults.
//
// .then rather than top-level await: the build target (es2020 and friends) does
// not allow the latter, and raising it to suit one call would narrow which
// browsers can run the app.
loadBranding().then(() => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </React.StrictMode>,
  )
})
