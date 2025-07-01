import { BottomPanel } from '@/components/panels/bottom/bottom-panel';
import { LeftSidebar } from '@/components/panels/left/left-sidebar';
import { RightSidebar } from '@/components/panels/right/right-sidebar';
import { TabBar } from '@/components/tabs/tab-bar';
import { TabContent } from '@/components/tabs/tab-content';
import { SidebarProvider } from '@/components/ui/sidebar';
import { FlowProvider, useFlowContext } from '@/contexts/flow-context';
import { TabsProvider, useTabsContext } from '@/contexts/tabs-context';
import { useLayoutKeyboardShortcuts } from '@/hooks/use-keyboard-shortcuts';
import { cn } from '@/lib/utils';
import { SidebarStorageService } from '@/services/sidebar-storage';
import { TabService } from '@/services/tab-service';
import { ReactFlowProvider } from '@xyflow/react';
import { ReactNode, useEffect, useState } from 'react';
import { TopBar } from './layout/top-bar';

type LayoutProps = {
  leftSidebar?: ReactNode;
  rightSidebar?: ReactNode;
  children: ReactNode;
  currentView?: string;
};

export function Layout({ leftSidebar, rightSidebar, children, currentView }: LayoutProps) {
  const [isCollapsed, setIsCollapsed] = useState(false); // Start expanded to show sidebar
  const [showCustomSidebar] = useState(true); // Show custom sidebar by default
  
  // Only show components sidebar in flow view
  const shouldShowComponentsSidebar = currentView === 'flow';

  return (
    <SidebarProvider defaultOpen={!isCollapsed}>
      <div className="flex h-screen w-screen overflow-hidden relative bg-background">
        <ReactFlowProvider>
          <FlowProvider>
            {/* Custom left sidebar for navigation */}
            {leftSidebar && showCustomSidebar && (
              <div className="h-full flex-shrink-0 z-30">
                {leftSidebar}
              </div>
            )}

            {/* Main content area */}
            <main className="flex-1 h-full overflow-hidden w-full">
              {children}
            </main>

            {/* Floating components sidebar - only show in flow view */}
            {shouldShowComponentsSidebar && (
              <div className={cn(
                "absolute top-0 right-0 z-30 h-full transition-transform",
                isCollapsed && "transform translate-x-full opacity-0"
              )}>
                <LeftSidebar
                  isCollapsed={isCollapsed}
                  onCollapse={() => setIsCollapsed(true)}
                  onExpand={() => setIsCollapsed(false)}
                  onToggleCollapse={() => setIsCollapsed(!isCollapsed)}
                />
              </div>
            )}

            {/* Components sidebar toggle button - visible when sidebar is collapsed and in flow view */}
            {(isCollapsed && shouldShowComponentsSidebar) && (
              <Button 
                className="absolute top-4 right-4 z-30 bg-ramp-grey-800 text-white p-2 rounded-md hover:bg-ramp-grey-700"
                onClick={() => setIsCollapsed(false)}
                aria-label="Show components sidebar"
              >
                Components <PanelLeft size={16} />
              </Button>
            )}

            {/* Right sidebar */}
            {rightSidebar && (
              <div className="h-full w-64 bg-gray-900 border-l border-gray-800 ml-auto flex-shrink-0">
                {rightSidebar}
              </div>
            )}
          </FlowProvider>
        </ReactFlowProvider>
      </div>

      {/* Main content area */}
      <main 
        className="absolute inset-0 overflow-hidden" 
        style={{
          left: !isLeftCollapsed ? `${leftSidebarWidth}px` : '0px',
          right: !isRightCollapsed ? `${rightSidebarWidth}px` : '0px',
          top: '40px', // Tab bar height
          bottom: !isBottomCollapsed ? `${bottomPanelHeight}px` : '0px',
        }}
      >
        <TabContent className="h-full w-full" />
      </main>

      {/* Floating left sidebar */}
      <div className={cn(
        "absolute top-0 left-0 z-30 h-full transition-transform",
        isLeftCollapsed && "transform -translate-x-full opacity-0"
      )}>
        <LeftSidebar
          isCollapsed={isLeftCollapsed}
          onCollapse={() => setIsLeftCollapsed(true)}
          onExpand={() => setIsLeftCollapsed(false)}
          onToggleCollapse={() => setIsLeftCollapsed(!isLeftCollapsed)}
          onWidthChange={setLeftSidebarWidth}
        />
      </div>

      {/* Floating right sidebar */}
      <div className={cn(
        "absolute top-0 right-0 z-30 h-full transition-transform",
        isRightCollapsed && "transform translate-x-full opacity-0"
      )}>
        <RightSidebar
          isCollapsed={isRightCollapsed}
          onCollapse={() => setIsRightCollapsed(true)}
          onExpand={() => setIsRightCollapsed(false)}
          onToggleCollapse={() => setIsRightCollapsed(!isRightCollapsed)}
          onWidthChange={setRightSidebarWidth}
        />
      </div>

      {/* Bottom panel */}
      <div 
        className={cn(
          "absolute bottom-0 z-20 transition-transform",
          isBottomCollapsed && "transform translate-y-full opacity-0"
        )}
        style={getSidebarBasedStyle()}
      >
        <BottomPanel
          isCollapsed={isBottomCollapsed}
          onCollapse={() => setIsBottomCollapsed(true)}
          onExpand={() => setIsBottomCollapsed(false)}
          onToggleCollapse={() => setIsBottomCollapsed(!isBottomCollapsed)}
          onHeightChange={setBottomPanelHeight}
        />
      </div>
    </div>
  );
}

type LayoutProps = {
  children?: ReactNode;
};

export function Layout({ children }: LayoutProps) {
  return (
    <SidebarProvider defaultOpen={true}>
      <ReactFlowProvider>
        <FlowProvider>
          <TabsProvider>
            <LayoutContent>{children}</LayoutContent>
          </TabsProvider>
        </FlowProvider>
      </ReactFlowProvider>
    </SidebarProvider>
  );
}