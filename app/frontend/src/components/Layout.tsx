import { BottomPanel } from '@/components/panels/bottom/bottom-panel';
import { LeftSidebar } from '@/components/panels/left/left-sidebar';
import { SidebarProvider } from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';
import { FlowProvider } from '@/contexts/flow-context';
import { TabsProvider } from '@/contexts/tabs-context';
import { cn } from '@/lib/utils';
import { ReactFlowProvider } from '@xyflow/react';
import { ReactNode, useState } from 'react';
import { PanelLeft } from 'lucide-react';

type LayoutProps = {
  leftSidebar?: ReactNode;
  rightSidebar?: ReactNode;
  children: ReactNode;
  currentView?: string;
};

export function Layout({ leftSidebar, rightSidebar, children, currentView }: LayoutProps) {
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [showCustomSidebar] = useState(true);
  
  // Only show components sidebar in flow view
  const shouldShowComponentsSidebar = currentView === 'flow';

  return (
    <SidebarProvider defaultOpen={!isCollapsed}>
      <div className="flex h-screen w-screen overflow-hidden relative bg-background">
        <ReactFlowProvider>
          <FlowProvider>
            <TabsProvider>
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

              {/* Bottom panel - only show in flow view */}
              {shouldShowComponentsSidebar && (
                <div className="absolute bottom-0 left-0 right-0 z-20">
                  <BottomPanel
                    isCollapsed={false}
                    onCollapse={() => {}}
                    onExpand={() => {}}
                    onToggleCollapse={() => {}}
                  />
                </div>
              )}
            </TabsProvider>
          </FlowProvider>
        </ReactFlowProvider>
      </div>
    </SidebarProvider>
  );
}