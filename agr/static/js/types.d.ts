// Development-only types for the classic scripts. This file is not loaded by the app.
// API payloads remain gradual `any` boundaries until their server schemas are typed.
interface AppState {
  readOnly: boolean; serverReadOnly: boolean;
  runId: string | null; view: string; chapter: string;
  forensic: any; review: any; sweep: any; queue: any; queueError: string | null;
  loadToken: number; loading: boolean; queueLoadToken: number;
  momentIdx: number; showAllContract: boolean; pendingStep: string | null;
  viewed: Set<string>; expandedMoments: Set<string>; expandedLessons: Set<string>;
  evidenceFocus: string | null; filters: Set<string>; sort: string; reviewer: string; dispOpen: boolean;
  projectId: string | null; projects: any[]; captureId: string | null; importRunIds: Set<string> | null;
  workspace: WorkspaceState;
  runsSearch: string; runsScroll: number; runsShown: number; runsGrouping: string;
  runsSelected: Set<string>; runsAnchorId: string | null;
  focusedQueue: any; focusSearch: string; reviewStarting: boolean;
  inspector: { runId: string | null; data: any; loading: boolean; error: string | null; token: number };
  compare: any; sibling: any; traceOpen: boolean; traceStep: string | null; entryPref: string;
  versions: { configurations: any; baseline: string | null; candidate: string | null;
    axis: string; keys: string[]; result: any; pending: boolean; savedId: string | null; defaultPair?: any };
  fleet: FleetState;
  ai: AIState;
  __booting?: boolean; __inPopstate?: boolean; navigationVersion?: number;
  inboxPromise?: Promise<void> | null; inboxRenderPending?: boolean; nextPending?: boolean;
  patternLabel?: string | null; patternRunIds?: Set<string> | null; patternSelector?: any;
  pendingSelection?: any; reviewerKey?: string | null; reviewDetailsOpen?: boolean;
  workflowOpens?: Map<string, Promise<any>>; workflowWrites?: Set<string>;
  focusGroupRevealed?: string | null; worklistGroupRevealed?: string | null;
  focusPages?: Map<string, {start: number; count: number}>;
}
interface WorkspaceState {
  source: string | null; files: {file: File; name: string; selected: boolean}[]; preview: any; job: any; busy: boolean;
  error: string | null; uploadProgress: any; selected: Set<string>; connections: any[];
  connectionId: string | null; investigations: any[]; detail: any; token: number;
  options?: any; batchLabel?: string; requestSignature?: string; requestKey?: string | null;
  catalog?: any; lf?: any; limits?: any; newProjectName?: string; operations?: any;
  projectDraft?: any; reviewerDraft?: any; startHere?: any; unavailable?: boolean;
}
interface AIState {
  models?: any; testResult?: any;
  settings: any; draft: any; error: string | null; selected: Set<string>;
  plan: any; job: any; jobs: any[]; force: boolean; token: number;
  requestId: string | null; filter: string; pollToken: number; watching: Set<string>;
}
interface FleetState {
  groupBy: string; episodes: any; pending: boolean; error: string | null; loadToken: number;
  usageSummary: any; executionQuality: any; total: number | null; hasMore: boolean; loadingMore: boolean;
  argumentShapes?: any; argumentShapesPending?: boolean; argumentShapesLoaded?: boolean;
  mode?: string; testGroupBy?: string; testGroups?: any; testPending?: boolean;
  testToken?: number; testError?: string | null; testHasMore?: boolean;
  testTotal?: number; testLoadingMore?: boolean; testFacets?: any;
}
