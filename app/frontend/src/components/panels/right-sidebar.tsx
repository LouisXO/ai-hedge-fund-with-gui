import { ComponentGroup, componentGroups as defaultComponentGroups } from '@/data/sidebar-components';
import { useComponentGroups } from '@/hooks/use-component-groups';
import { useResizable } from '@/hooks/use-resizable';
import { cn } from '@/lib/utils';
import { ReactNode, useEffect, useState } from 'react';
import { ComponentActions } from './right/component-actions';
import { ComponentList } from './right/component-list';

interface RightSidebarProps {
  children?: ReactNode;
  isCollapsed: boolean;
  onCollapse: () => void;
  onExpand: () => void;
  onWidthChange?: (width: number) => void;
}

export function RightSidebar({
  isCollapsed,
  onWidthChange,
}: RightSidebarProps) {
  // Use our custom hooks
  const { width, isDragging, elementRef, startResize } = useResizable({
    defaultWidth: 280,
    minWidth: 200,
    maxWidth: 500,
    side: 'right',
  });
  
  // Notify parent component of width changes
  useEffect(() => {
    onWidthChange?.(width);
  }, [width, onWidthChange]);
  
  // State for loading component groups
  const [componentGroups, setComponentGroups] = useState<ComponentGroup[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  
  // Load component groups on mount
  useEffect(() => {
    const loadComponentGroups = async () => {
      try {
        setIsLoading(true);
        const groups = defaultComponentGroups;
        setComponentGroups(groups);
      } catch (error) {
        console.error('Failed to load component groups:', error);
      } finally {
        setIsLoading(false);
      }
    };
    
    loadComponentGroups();
  }, []);
  
  const { 
    searchQuery, 
    setSearchQuery, 
    activeItem, 
    openGroups, 
    filteredGroups,
    handleAccordionChange 
  } = useComponentGroups(componentGroups);

  return (
    <div 
      ref={elementRef}
      className={cn(
        "h-full bg-panel flex flex-col relative",
        isCollapsed ? "shadow-lg" : "",
        isDragging ? "select-none" : ""
      )}
      style={{ 
        width: `${width}px`,
        borderLeft: isDragging ? 'none' : '1px solid var(--ramp-grey-900)' 
      }}
    >
              <ComponentActions />
      
      <ComponentList
        componentGroups={componentGroups}
        searchQuery={searchQuery}
        isLoading={isLoading}
        openGroups={openGroups}
        filteredGroups={filteredGroups}
        activeItem={activeItem}
        onSearchChange={setSearchQuery}
        onAccordionChange={handleAccordionChange}
      />
      
      {/* Resize handle - on the left side for right sidebar */}
      {!isDragging && (
        <div 
          className="absolute top-0 left-0 h-full w-1 cursor-ew-resize transition-all duration-150 z-10"
          onMouseDown={startResize}
        />
      )}
    </div>
  );
} 