import { Headphones, Library, BookOpen } from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { SystemBanner } from '@/components/SystemBanner'

export function Layout() {
  return (
    <div className="min-h-screen flex flex-col bg-background text-foreground">
      <header className="border-b">
        <div className="mx-auto max-w-6xl px-6 py-4 flex items-center gap-6">
          <NavLink to="/" className="flex items-center gap-2 font-semibold">
            <Headphones className="h-5 w-5 text-primary" strokeWidth={1.75} />
            <span>audiomat</span>
            <span className="text-xs text-muted-foreground font-normal">v0.1</span>
          </NavLink>
          <nav className="flex items-center gap-1 ml-auto text-sm">
            <NavItem to="/projects" icon={<BookOpen className="h-4 w-4" />}>
              Projects
            </NavItem>
            <NavItem to="/voices" icon={<Library className="h-4 w-4" />}>
              Voices
            </NavItem>
          </nav>
        </div>
      </header>

      <SystemBanner />

      <main className="flex-1">
        <div className="mx-auto max-w-6xl px-6 py-8">
          <Outlet />
        </div>
      </main>

      <footer className="border-t text-xs text-muted-foreground">
        <div className="mx-auto max-w-6xl px-6 py-3 flex items-center justify-between">
          <span>audiomat — eBook → audiobook with cloned voices</span>
          <span className="font-mono">OmniVoice · Apache-2.0</span>
        </div>
      </footer>
    </div>
  )
}

function NavItem({
  to,
  icon,
  children,
}: {
  to: string
  icon: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        [
          'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md transition-colors',
          isActive
            ? 'bg-secondary text-secondary-foreground'
            : 'hover:bg-secondary/50 text-muted-foreground hover:text-foreground',
        ].join(' ')
      }
    >
      {icon}
      {children}
    </NavLink>
  )
}
