def get_or_create_new_chat_session(
    db_session: Session, 
    user_id: UUID_ID,
    new_msg_req: CreateChatMessageRequest
) -> ChatSession:
    if new_msg_req.chat_session_id is None:
        # If the end user start a new chat
        chat_session = create_chat_session(
                db_session=db_session, 
                description="",
                persona_id=new_msg_req.persona_id,
                user_id=user_id,
            )
    else:
        # If the end user is responding to an existing chat
        chat_session = get_chat_session_by_id(
                db_session=db_session,
                user_id=user_id,
                chat_session_id=new_msg_req.chat_session_id,
            )
        
    return chat_session
