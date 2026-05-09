import { Headphones } from 'lucide-react'

function App() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-background text-foreground">
      <div className="flex flex-col items-center gap-6 p-8 text-center">
        <Headphones className="h-16 w-16 text-primary" strokeWidth={1.25} />
        <div>
          <h1 className="text-5xl font-bold tracking-tight">audiomat</h1>
          <p className="mt-2 text-muted-foreground">
            eBook to audiobook with cloned voices.
          </p>
        </div>
        <div className="rounded-lg border bg-card px-6 py-4 text-sm text-muted-foreground">
          <p className="font-medium text-card-foreground">Pre-alpha</p>
          <p className="mt-1">UI scaffolding in progress.</p>
        </div>
      </div>
    </div>
  )
}

export default App
