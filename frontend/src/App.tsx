import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from '@/components/Layout'
import { Landing } from '@/pages/Landing'
import { Voices } from '@/pages/Voices'
import { VoiceNew } from '@/pages/VoiceNew'
import { Projects } from '@/pages/Projects'
import { ProjectNew } from '@/pages/ProjectNew'
import { ProjectDetail } from '@/pages/ProjectDetail'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Landing />} />
          <Route path="/voices" element={<Voices />} />
          <Route path="/voices/new" element={<VoiceNew />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/projects/new" element={<ProjectNew />} />
          <Route path="/projects/:slug" element={<ProjectDetail />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
