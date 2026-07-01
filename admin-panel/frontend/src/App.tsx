import { BrowserRouter, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import AuditLog from './pages/AuditLog';
import Chat from './pages/Chat';
import Content from './pages/Content';
import ContentDetail from './pages/ContentDetail';
import Flows from './pages/Flows';
import Homelab from './pages/Homelab';
import Infra from './pages/Infra';
import Integrations from './pages/Integrations';
import InteractionDetail from './pages/InteractionDetail';
import Interactions from './pages/Interactions';
import Knowledge from './pages/Knowledge';
import ModelsProviders from './pages/ModelsProviders';
import Market from './pages/Market';
import Money from './pages/Money';
import Todoist from './pages/Todoist';
import Overview from './pages/Overview';
import PersonalityDetail from './pages/PersonalityDetail';
import Personalities from './pages/Personalities';
import References from './pages/References';
import Resources from './pages/Resources';
import Workflows from './pages/Workflows';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Overview />} />
          <Route path="/interactions" element={<Interactions />} />
          <Route path="/interactions/:id" element={<InteractionDetail />} />
          <Route path="/workflows" element={<Workflows />} />
          <Route path="/flows" element={<Flows />} />
          <Route path="/models" element={<ModelsProviders />} />
          <Route path="/integrations" element={<Integrations />} />
          <Route path="/resources" element={<Resources />} />
          <Route path="/personalities" element={<Personalities />} />
          <Route path="/personalities/:id" element={<PersonalityDetail />} />
          <Route path="/market" element={<Market />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/knowledge" element={<Knowledge />} />
          <Route path="/content" element={<Content />} />
          <Route path="/content/:id" element={<ContentDetail />} />
          <Route path="/references" element={<References />} />
          <Route path="/audit" element={<AuditLog />} />
          <Route path="/infra" element={<Infra />} />
          <Route path="/admin/homelab" element={<Homelab />} />
          <Route path="/admin/money" element={<Money />} />
          <Route path="/admin/todoist" element={<Todoist />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
