import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components";
import { Chat } from "./pages/Chat";
import { Dashboard } from "./pages/Dashboard";
import { Activity, Agents, Approvals, Artifacts, Briefings, Insights, Memory, Schedule, SettingsPage, SystemPage, Tasks } from "./pages/Verbose";

export function App() {
  return <Routes><Route element={<Layout/>}>
    <Route path="/" element={<Dashboard/>}/>
    <Route path="/chat" element={<Chat/>}/>
    <Route path="/chat/:conversationId" element={<Chat/>}/>
    <Route path="/tasks" element={<Tasks/>}/>
    <Route path="/briefings" element={<Briefings/>}/>
    <Route path="/artifacts" element={<Artifacts/>}/>
    <Route path="/schedule" element={<Schedule/>}/>
    <Route path="/approvals" element={<Approvals/>}/>
    <Route path="/memory" element={<Memory/>}/>
    <Route path="/agents" element={<Agents/>}/>
    <Route path="/activity" element={<Activity/>}/>
    <Route path="/insights" element={<Insights/>}/>
    <Route path="/system" element={<SystemPage/>}/>
    <Route path="/settings" element={<SettingsPage/>}/>
    <Route path="*" element={<Navigate to="/" replace/>}/>
  </Route></Routes>;
}
