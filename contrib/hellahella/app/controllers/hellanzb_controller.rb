class HellanzbController < ApplicationController
  before_filter :authorize, :defaults
  before_filter :load_queue, :except => :index
  before_filter :load_status, :except => :queue
  
  def index
    @asciiart = server.call('asciiart')
  end
  
  def queue
  end

  def dequeue
    nzb_id = params[:id].split("_")[1]
    server.call('dequeue', nzb_id)
    load_queue
    render :partial => "queue_items"
  end
    
  def bandwidth
    if request.post?
      server.call('maxrate', params[:maxrate])
      session[:status] = nil
      load_status
    end
  end
  
  def enqueue_bookmarklet
    @id = params[:url].split('/')[-1]
    server.call('enqueuenewzbin', @id)
    redirect_to(params[:url])
  end
  
  def bookmarklet
      @mylink = "%s%s:%s" % [request.protocol,request.host,request.port]
  end
end