import { createRoot } from 'react-dom/client'

function SandboxPlaceholder() {
  return <div data-vault-sandbox-ready="false" />
}

createRoot(document.getElementById('root') as HTMLElement).render(<SandboxPlaceholder />)
