import { Navigate, Route, Routes, useParams } from "react-router-dom";
import { Layout } from "./components";
import { Chat } from "./pages/Chat";
import { Dashboard } from "./pages/Dashboard";
import { Agents, Approvals, Artifacts, Insights, Memory, Schedule, SettingsPage, Skills, SystemPage } from "./pages/Verbose";
import { Tasks } from "./pages/Tasks";
import { DialogProvider } from "./dialog";

/** Keeps the task when a bookmarked /work/<id> is followed to its new home. */
function WorkRedirect() {
  const { taskId } = useParams();
  return <Navigate to={`/tasks/${taskId}`} replace/>;
}

export function App() {
  return <DialogProvider><Routes><Route element={<Layout/>}>
    <Route path="/" element={<Dashboard/>}/>
    <Route path="/chat" element={<Chat/>}/>
    <Route path="/chat/:conversationId" element={<Chat/>}/>
    <Route path="/briefings" element={<Navigate to="/artifacts" replace/>}/>
    <Route path="/artifacts" element={<Artifacts/>}/>
    <Route path="/schedule" element={<Schedule/>}/>
    <Route path="/approvals" element={<Approvals/>}/>
    <Route path="/memory" element={<Memory/>}/>
    <Route path="/agents" element={<Agents/>}/>
    <Route path="/skills" element={<Skills/>}/>
    {/* Tasks and Activity were one thing described in two places. Merged, with
        the event stream a tab inside the task list rather than a page beside
        it. The old paths redirect: they are in muscle memory and in bookmarks,
        and the same courtesy is already paid to /briefings and /insights. */}
    <Route path="/tasks" element={<Tasks/>}/>
    <Route path="/tasks/:taskId" element={<Tasks/>}/>
    <Route path="/activity" element={<Navigate to="/tasks?view=everything" replace/>}/>
    <Route path="/work" element={<Navigate to="/tasks" replace/>}/>
    <Route path="/work/:taskId" element={<WorkRedirect/>}/>
    <Route path="/bootstrap" element={<Navigate to="/memory" replace/>}/>
    <Route path="/insights" element={<Navigate to="/system" replace/>}/>
    <Route path="/system" element={<SystemPage/>}/>
    <Route path="/settings" element={<SettingsPage/>}/>
    <Route path="*" element={<Navigate to="/" replace/>}/>
  </Route></Routes></DialogProvider>;
}
